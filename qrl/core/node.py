# coding=utf-8
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.
import threading
import time
from twisted.internet import reactor
from pyqrllib.pyqrllib import bin2hstr

from qrl.core import config
from qrl.core.Miner import Miner
from qrl.core.ChainManager import ChainManager
from qrl.core.misc import ntp, logger
from qrl.core.Block import Block
from qrl.core.ESyncState import ESyncState


class SyncState:
    def __init__(self):
        self.state = ESyncState.unsynced
        self.epoch_diff = -1


class ConsensusMechanism(object):
    def __init__(self,
                 chain_manager: ChainManager):
        self.chain_manager = chain_manager


class POW(ConsensusMechanism):
    def __init__(self,
                 chain_manager: ChainManager,
                 p2p_factory,
                 sync_state: SyncState,
                 time_provider,
                 slaves: list):

        super().__init__(chain_manager)
        self.sync_state = sync_state
        self.time_provider = time_provider

        self.slaves = slaves

        self.p2p_factory = p2p_factory  # FIXME: Decouple from p2pFactory. Comms vs node logic
        self.p2p_factory.pow = self  # FIXME: Temporary hack to keep things working while refactoring

        self.miner = Miner(self.pre_block_logic,
                           self.slaves,
                           self.chain_manager.state,
                           self.p2p_factory.add_unprocessed_txn)

        self._miner_lock = threading.Lock()

        ########

        self.last_pow_cycle = 0
        self.last_bk_time = 0
        self.last_pb_time = 0

        self.epoch_diff = None

    ##################################################
    ##################################################
    ##################################################
    ##################################################

    def start(self):
        self.restart_monitor_bk(80)
        reactor.callLater(20, self.initialize_pow)

    def _handler_state_unsynced(self):
        self.miner.cancel()
        self.last_bk_time = time.time()
        self.restart_unsynced_logic()

    def _handler_state_syncing(self):
        self.miner.cancel()
        self.last_pb_time = time.time()

    def _handler_state_synced(self):
        self.last_pow_cycle = time.time()
        last_block = self.chain_manager.last_block
        self.mine_next(last_block)

    def _handler_state_forked(self):
        pass

    def update_node_state(self, new_sync_state: ESyncState):
        self.sync_state.state = new_sync_state
        logger.info('Status changed to %s', self.sync_state.state)

        _mapping = {
            ESyncState.unsynced: self._handler_state_unsynced,
            ESyncState.syncing: self._handler_state_syncing,
            ESyncState.synced: self._handler_state_synced,
            ESyncState.forked: self._handler_state_forked,
        }

        _mapping[self.sync_state.state]()

    def stop_monitor_bk(self):
        try:
            reactor.monitor_bk.cancel()
        except Exception:  # No need to log this exception
            pass

    def restart_monitor_bk(self, delay: int):
        self.stop_monitor_bk()
        reactor.monitor_bk = reactor.callLater(delay, self.monitor_bk)

    def monitor_bk(self):
        # FIXME: Too many magic numbers / timing constants
        # FIXME: This is obsolete
        time_diff1 = time.time() - self.last_pow_cycle
        if 90 < time_diff1:
            if self.sync_state.state == ESyncState.unsynced:
                if time.time() - self.last_bk_time > 120:
                    self.last_pow_cycle = time.time()
                    logger.info(' POW cycle activated by monitor_bk() ')
                    self.update_node_state(ESyncState.synced)
                reactor.monitor_bk = reactor.callLater(60, self.monitor_bk)
                return

        time_diff2 = time.time() - self.last_pb_time
        if self.sync_state.state == ESyncState.syncing and time_diff2 > 60:
            self.update_node_state(ESyncState.unsynced)
            self.epoch_diff = -1

        reactor.monitor_bk = reactor.callLater(60, self.monitor_bk)

    def initialize_pow(self):
        reactor.callLater(30, self.update_node_state, ESyncState.synced)

    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################

    def restart_unsynced_logic(self, delay=0):
        logger.info('Restarting unsynced logic in %s seconds', delay)
        try:
            reactor.unsynced_logic.cancel()
        except Exception:  # No need to log this exception
            pass

        reactor.unsynced_logic = reactor.callLater(delay, self.unsynced_logic)

    def unsynced_logic(self):
        if self.sync_state.state != ESyncState.synced:
            self.p2p_factory.broadcast_get_synced_state()
            reactor.request_peer_blockheight = reactor.callLater(0, self.p2p_factory.request_peer_blockheight)
            reactor.unsynced_logic = reactor.callLater(20, self.start_download)

    def start_download(self):
        # FIXME: Why PoW is downloading blocks?
        # add peers and their identity to requested list
        # FMBH
        if self.sync_state.state == ESyncState.synced:
            return

        logger.info('Checking Download..')

        if self.p2p_factory.connections == 0:
            logger.warning('No connected peers. Moving to synced state')
            self.update_node_state(ESyncState.synced)
            return

        self.update_node_state(ESyncState.syncing)
        logger.info('Initializing download from %s', self.chain_manager.height + 1)
        self.p2p_factory.randomize_block_fetch()

    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################
    ##############################################

    def pre_block_logic(self, block: Block):
        logger.debug('Checking miner lock')
        with self._miner_lock:
            logger.debug('Inside add_block')
            result = self.chain_manager.add_block(block)

            logger.debug('Checking trigger_miner %s', self.chain_manager.trigger_miner)
            if self.chain_manager.trigger_miner or not self.miner.isRunning():
                self.mine_next(self.chain_manager.last_block)

            if not result:
                logger.debug('Block Rejected %s %s', block.block_number, bin2hstr(block.headerhash))
                return

            reactor.callLater(0, self.broadcast_block, block)

    def broadcast_block(self, block):
        if self.sync_state.state == ESyncState.synced:
            self.p2p_factory.broadcast_block(block)

    def isSynced(self, block_timestamp) -> bool:
        if block_timestamp + config.dev.minimum_minting_delay > ntp.getTime():
            self.update_node_state(ESyncState.synced)
            return True
        return False

    def mine_next(self, parent_block):
        if config.user.mining_enabled:
            parent_metadata = self.chain_manager.state.get_block_metadata(parent_block.headerhash)
            logger.info('Mining Block #%s', parent_block.block_number + 1)
            self.miner.start_mining(self.chain_manager.tx_pool, parent_block, parent_metadata.block_difficulty)
