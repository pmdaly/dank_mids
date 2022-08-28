
import asyncio
from collections import defaultdict
from time import time
from typing import Any, List, Set

import multicall
from eth_utils import to_checksum_address
from web3 import Web3
from web3.providers import HTTPProvider
from web3.providers.async_base import AsyncBaseProvider
from web3.types import RPCEndpoint, RPCResponse

from dank_mids._config import LOOP_INTERVAL
from dank_mids.call import BatchedCall
from dank_mids.loggers import (demo_logger, main_logger, sort_lazy_logger,
                               sort_logger)
from dank_mids.uid import UIDGenerator
from dank_mids.worker import DankWorker

instances: List["DankMiddlewareController"] = []


class DankMiddlewareController:
    def __init__(self, w3: Web3) -> None:
        assert w3.eth.is_async and isinstance(w3.provider, AsyncBaseProvider), "Dank Middleware can only be applied to an asycnhronous Web3 instance."
        self.w3: Web3 = w3
        self.sync_w3: Web3 = Web3(provider = HTTPProvider(self.w3.provider.endpoint_uri))
        # Can't pickle middlewares to send to process executor
        self.sync_w3.middleware_onion.clear()
        self.sync_w3.provider.middlewares = tuple()
        self.DO_NOT_BATCH: Set[str] = set()
        self.pending_calls: List[BatchedCall] = []
        self.num_pending_calls: int = 0
        self.worker = DankWorker(self)
        self.is_running: bool = False
        self.call_uid = UIDGenerator()
        self._initializing: bool = False
        self._is_configured: bool = False
        self._pools_closed: bool = False
        self._checkpoint: float = time()
        self._instance: int = len(instances)
        instances.append(self)
    
    def __repr__(self) -> str:
        return f"<DankMiddlewareController {self._instance}>"

    async def __call__(self, params: Any) -> RPCResponse:
        if not self._is_configured:
            await self._setup()
        call = await self.add_to_queue(params)
        return await call
    
    @property
    def batcher(self):
        return self.worker.batcher
    
    async def taskmaster_loop(self) -> None:
        self.is_running = True
        while self.pending_calls:
            await asyncio.sleep(0)
            if (self.loop_is_ready or self.queue_is_full):
                await self.execute_multicall()
        self.is_running = False
    
    async def execute_multicall(self) -> None:
        i = 0
        while self.call_uid.lock.locked():
            if i // 500 == int(i // 500):
                main_logger.debug('lock is locked')
            i += 1
            await asyncio.sleep(.1)
        self._pools_closed = True
        with self.call_uid.lock:
            calls_to_exec = defaultdict(list)
            for call in self.pending_calls:
                calls_to_exec[call.block].append(call)
            self.pending_calls.clear()
            self.num_pending_calls = 0
        self._pools_closed = False
        demo_logger.info(f'executing multicall (current cid: {self.call_uid.latest})')
        await self.worker.execute_multicall(calls_to_exec)

    @sort_lazy_logger
    def should_batch(self, method: RPCEndpoint, params: Any) -> bool:
        """ Determines whether or not a call should be passed to the DankMiddlewareController. """
        if method != 'eth_call':
            sort_logger.debug(f"bypassed, method is {method}")
            return False
        elif params[0]['to'] in self.DO_NOT_BATCH:
            sort_logger.debug(f"bypassed, target is in `DO_NOT_BATCH`")
            return False
        return True
    
    async def add_to_queue(self, params: Any) -> "BatchedCall":
        """ Adds a call to the DankMiddlewareContoller's `pending_calls`. """
        while self._pools_closed:
            await asyncio.sleep(0)
        return BatchedCall(self, params)

    @property
    def loop_is_ready(self) -> bool:
        return time() - self._checkpoint > LOOP_INTERVAL
    
    @property
    def queue_is_full(self) -> bool:
        return bool(len(self.pending_calls) >= self.batcher.step * 25)

    async def _setup(self) -> None:
        if self._initializing:
            while self._initializing:
                await asyncio.sleep(0)
            return
        self._initializing = True
        main_logger.info('Dank Middleware initializing... Strap on your rocket boots...')
        # NOTE use sync w3 here to prevent timeout issues with abusive scripts.
        chain_id = self.sync_w3.eth.chain_id
        MULTICALL = multicall.constants.MULTICALL_ADDRESSES.get(chain_id,None)
        self.MULTICALL2 = multicall.constants.MULTICALL2_ADDRESSES.get(chain_id,None)
        self.DO_NOT_BATCH.update(to_checksum_address(address) for address in [MULTICALL,self.MULTICALL2] if address)
        self._is_configured = True
        self._initializing = False
