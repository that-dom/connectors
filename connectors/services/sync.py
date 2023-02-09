#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""
Event loop

- polls for work by calling Elasticsearch on a regular basis
- instantiates connector plugins
- mirrors an Elasticsearch index with a collection of documents
"""
import asyncio
import functools

from connectors.byoc import (
    ConnectorIndex,
    ConnectorUpdateError,
    DataSourceError,
    ServiceTypeNotConfiguredError,
    ServiceTypeNotSupportedError,
    Status,
)
from connectors.byoei import ElasticServer
from connectors.filtering.validation import (
    InvalidFilteringError,
    ValidationTarget,
    validate_filtering,
)
from connectors.logger import logger
from connectors.services.base import BaseService
from connectors.utils import ConcurrentTasks, e2str

DEFAULT_MAX_CONCURRENT_SYNCS = 1


class SyncService(BaseService):
    def __init__(self, config):
        super().__init__(config)
        self.idling = self.service_config["idling"]
        self.hb = self.service_config["heartbeat"]
        self.concurrent_syncs = self.service_config.get(
            "max_concurrent_syncs", DEFAULT_MAX_CONCURRENT_SYNCS
        )
        self.connectors = None
        self.syncs = None

    async def stop(self):
        await super().stop()
        if self.syncs is not None:
            self.syncs.cancel()

    async def _one_sync(self, connector, es):
        if self.running is False:
            logger.debug(
                f"Skipping run for {connector.id} because service is terminating"
            )
            return

        if connector.native:
            logger.debug(f"Connector {connector.id} natively supported")

        try:
            await connector.prepare(self.config)
        except ServiceTypeNotConfiguredError:
            logger.error(
                f"Service type is not configured for connector {self.config['connector_id']}"
            )
            return
        except ConnectorUpdateError as e:
            logger.error(e)
            return
        except ServiceTypeNotSupportedError:
            logger.debug(f"Can't handle source of type {connector.service_type}")
            return
        except DataSourceError as e:
            await connector.error(e)
            logger.critical(e, exc_info=True)
            self.raise_if_spurious(e)
            return

        try:
            # the heartbeat is always triggered
            connector.start_heartbeat(self.hb)

            logger.debug(f"Connector status is {connector.status}")

            # we trigger a sync
            if connector.status in (Status.CREATED, Status.NEEDS_CONFIGURATION):
                # we can't sync in that state
                logger.info(f"Can't sync with status `{e2str(connector.status)}`")
            else:
                if connector.features.sync_rules_enabled():
                    await validate_filtering(
                        connector, self.connectors, ValidationTarget.DRAFT
                    )

                await connector.sync(es, self.idling)

            await asyncio.sleep(0)
        except InvalidFilteringError as e:
            logger.error(e)
            return
        finally:
            await connector.close()

    async def _run(self):
        """Main event loop."""
        self.connectors = ConnectorIndex(self.es_config)

        native_service_types = self.config.get("native_service_types", [])
        logger.debug(f"Native support for {', '.join(native_service_types)}")

        # XXX we can support multiple connectors but Ruby can't so let's use a
        # single id
        # connectors_ids = self.config.get("connectors_ids", [])
        if "connector_id" in self.config:
            connectors_ids = [self.config.get("connector_id")]
        else:
            connectors_ids = []

        logger.info(
            f"Service started, listening to events from {self.es_config['host']}"
        )

        es = ElasticServer(self.es_config)
        try:
            while self.running:
                # creating a pool of task for every round
                self.syncs = ConcurrentTasks(max_concurrency=self.concurrent_syncs)

                try:
                    logger.debug(f"Polling every {self.idling} seconds")
                    query = self.connectors.build_docs_query(
                        native_service_types, connectors_ids
                    )
                    async for connector in self.connectors.get_all_docs(query=query):
                        await self.syncs.put(
                            functools.partial(self._one_sync, connector, es)
                        )
                except Exception as e:
                    logger.critical(e, exc_info=True)
                    self.raise_if_spurious(e)
                finally:
                    await self.syncs.join()

                self.syncs = None
                # Immediately break instead of sleeping
                if not self.running:
                    break
                await self._sleeps.sleep(self.idling)
        finally:
            if self.connectors is not None:
                self.connectors.stop_waiting()
                await self.connectors.close()
            await es.close()
        return 0