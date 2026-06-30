import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the path from https:///github.com/dfinity/ICRC-1
SOURCE_REPO = "onflow/flow-go"
# todo: the name of the repository
REPO_NAME = "flow-go"
run_number = os.environ.get('GITHUB_RUN_NUMBER') or os.environ.get('CI_PIPELINE_IID', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"

scope_files = [
    'access/api.go',
    'access/backends/extended/api.go',
    'access/backends/extended/backend.go',
    'access/backends/extended/backend_account_transactions.go',
    'access/backends/extended/backend_account_transfers.go',
    'access/backends/extended/backend_base.go',
    'access/backends/extended/backend_contracts.go',
    'access/backends/extended/backend_scheduled_transactions.go',
    'access/legacy/convert/convert.go',
    'access/legacy/handler.go',
    'access/utils/cadence.go',
    'access/validator/errors.go',
    'access/validator/validator.go',
    'consensus/aggregators.go',
    'consensus/config.go',
    'consensus/follower.go',
    'consensus/hotstuff/block_producer.go',
    'consensus/hotstuff/blockproducer/block_producer.go',
    'consensus/hotstuff/blockproducer/safety_rules_wrapper.go',
    'consensus/hotstuff/committee.go',
    'consensus/hotstuff/committees/cluster_committee.go',
    'consensus/hotstuff/committees/consensus_committee.go',
    'consensus/hotstuff/committees/leader/bootstrap.go',
    'consensus/hotstuff/committees/leader/cluster.go',
    'consensus/hotstuff/committees/leader/consensus.go',
    'consensus/hotstuff/committees/leader/leader_selection.go',
    'consensus/hotstuff/committees/static.go',
    'consensus/hotstuff/committees/threshold.go',
    'consensus/hotstuff/consumer.go',
    'consensus/hotstuff/cruisectl/aggregators.go',
    'consensus/hotstuff/cruisectl/block_time_controller.go',
    'consensus/hotstuff/cruisectl/config.go',
    'consensus/hotstuff/cruisectl/proposal_timing.go',
    'consensus/hotstuff/event_handler.go',
    'consensus/hotstuff/event_loop.go',
    'consensus/hotstuff/eventhandler/event_handler.go',
    'consensus/hotstuff/eventloop/event_loop.go',
    'consensus/hotstuff/finalization_registrar.go',
    'consensus/hotstuff/follower_loop.go',
    'consensus/hotstuff/forks.go',
    'consensus/hotstuff/forks/blockcontainer.go',
    'consensus/hotstuff/forks/forks.go',
    'consensus/hotstuff/helper/block.go',
    'consensus/hotstuff/helper/bls_key.go',
    'consensus/hotstuff/helper/quorum_certificate.go',
    'consensus/hotstuff/helper/signature.go',
    'consensus/hotstuff/helper/timeout_certificate.go',
    'consensus/hotstuff/model/block.go',
    'consensus/hotstuff/model/errors.go',
    'consensus/hotstuff/model/proposal.go',
    'consensus/hotstuff/model/signature_data.go',
    'consensus/hotstuff/model/timeout.go',
    'consensus/hotstuff/model/vote.go',
    'consensus/hotstuff/notifications/pubsub/communicator_distributor.go',
    'consensus/hotstuff/notifications/pubsub/distributor.go',
    'consensus/hotstuff/notifications/pubsub/finalization_distributor.go',
    'consensus/hotstuff/notifications/pubsub/participant_distributor.go',
    'consensus/hotstuff/notifications/pubsub/proposal_violation_distributor.go',
    'consensus/hotstuff/notifications/pubsub/timeout_aggregation_violation_consumer.go',
    'consensus/hotstuff/notifications/pubsub/timeout_collector_distributor.go',
    'consensus/hotstuff/notifications/pubsub/vote_aggregation_violation_consumer.go',
    'consensus/hotstuff/notifications/pubsub/vote_collector_distributor.go',
    'consensus/hotstuff/notifications/slashing_violation_consumer.go',
    'consensus/hotstuff/pacemaker.go',
    'consensus/hotstuff/pacemaker/pacemaker.go',
    'consensus/hotstuff/pacemaker/proposal_timing.go',
    'consensus/hotstuff/pacemaker/timeout/config.go',
    'consensus/hotstuff/pacemaker/timeout/controller.go',
    'consensus/hotstuff/pacemaker/view_tracker.go',
    'consensus/hotstuff/persister.go',
    'consensus/hotstuff/persister/persister.go',
    'consensus/hotstuff/randombeacon_inspector.go',
    'consensus/hotstuff/safety_rules.go',
    'consensus/hotstuff/safetyrules/safety_rules.go',
    'consensus/hotstuff/signature.go',
    'consensus/hotstuff/signature/block_signer_decoder.go',
    'consensus/hotstuff/signature/packer.go',
    'consensus/hotstuff/signature/randombeacon_inspector.go',
    'consensus/hotstuff/signature/randombeacon_reconstructor.go',
    'consensus/hotstuff/signature/randombeacon_signer_store.go',
    'consensus/hotstuff/signature/static_randombeacon_signer_store.go',
    'consensus/hotstuff/signature/weighted_signature_aggregator.go',
    'consensus/hotstuff/signer.go',
    'consensus/hotstuff/timeout_aggregator.go',
    'consensus/hotstuff/timeout_collector.go',
    'consensus/hotstuff/timeout_collectors.go',
    'consensus/hotstuff/timeoutaggregator/timeout_aggregator.go',
    'consensus/hotstuff/timeoutaggregator/timeout_collectors.go',
    'consensus/hotstuff/timeoutcollector/aggregation.go',
    'consensus/hotstuff/timeoutcollector/factory.go',
    'consensus/hotstuff/timeoutcollector/timeout_cache.go',
    'consensus/hotstuff/timeoutcollector/timeout_collector.go',
    'consensus/hotstuff/timeoutcollector/timeout_processor.go',
    'consensus/hotstuff/tracker/tracker.go',
    'consensus/hotstuff/validator.go',
    'consensus/hotstuff/validator/validator.go',
    'consensus/hotstuff/verification/combined_signer_v2.go',
    'consensus/hotstuff/verification/combined_signer_v3.go',
    'consensus/hotstuff/verification/combined_verifier_v2.go',
    'consensus/hotstuff/verification/combined_verifier_v3.go',
    'consensus/hotstuff/verification/common.go',
    'consensus/hotstuff/verification/staking_signer.go',
    'consensus/hotstuff/verification/staking_verifier.go',
    'consensus/hotstuff/verifier.go',
    'consensus/hotstuff/vote_aggregator.go',
    'consensus/hotstuff/vote_collector.go',
    'consensus/hotstuff/vote_collectors.go',
    'consensus/hotstuff/voteaggregator/pending_status.go',
    'consensus/hotstuff/voteaggregator/vote_aggregator.go',
    'consensus/hotstuff/voteaggregator/vote_collectors.go',
    'consensus/hotstuff/votecollector/combined_vote_processor_v2.go',
    'consensus/hotstuff/votecollector/combined_vote_processor_v3.go',
    'consensus/hotstuff/votecollector/common.go',
    'consensus/hotstuff/votecollector/factory.go',
    'consensus/hotstuff/votecollector/staking_vote_processor.go',
    'consensus/hotstuff/votecollector/statemachine.go',
    'consensus/hotstuff/votecollector/vote_cache.go',
    'consensus/participant.go',
    'consensus/recovery/cluster/state.go',
    'consensus/recovery/protocol/state.go',
    'consensus/recovery/recover.go',
    'engine/access/apiproxy/access_api_proxy.go',
    'engine/access/index/events_index.go',
    'engine/access/index/transaction_results_indexer.go',
    'engine/access/ingestion/collections/indexer.go',
    'engine/access/ingestion/collections/syncer.go',
    'engine/access/ingestion/collections/syncer_execution_data.go',
    'engine/access/ingestion/engine.go',
    'engine/access/ingestion/tx_error_messages/requester.go',
    'engine/access/ingestion/tx_error_messages/tx_error_messages_core.go',
    'engine/access/ingestion/tx_error_messages/tx_error_messages_engine.go',
    'engine/access/ingestion2/engine.go',
    'engine/access/ingestion2/finalized_block_processor.go',
    'engine/access/rest/apiproxy/rest_proxy_handler.go',
    'engine/access/rest/common/error.go',
    'engine/access/rest/common/http_request_handler.go',
    'engine/access/rest/common/parser/address.go',
    'engine/access/rest/common/parser/arguments.go',
    'engine/access/rest/common/parser/block_status.go',
    'engine/access/rest/common/parser/event_type.go',
    'engine/access/rest/common/parser/id.go',
    'engine/access/rest/common/parser/proposal_key.go',
    'engine/access/rest/common/parser/signatures.go',
    'engine/access/rest/common/parser/transaction.go',
    'engine/access/rest/common/request.go',
    'engine/access/rest/common/utils.go',
    'engine/access/rest/experimental/get_account_transactions.go',
    'engine/access/rest/experimental/handler.go',
    'engine/access/rest/experimental/request/cursor_contracts.go',
    'engine/access/rest/experimental/request/cursor_scheduled_transactions.go',
    'engine/access/rest/experimental/request/cursor_transfer.go',
    'engine/access/rest/experimental/request/get_account_ft_transfers.go',
    'engine/access/rest/experimental/request/get_account_nft_transfers.go',
    'engine/access/rest/experimental/request/get_account_transactions.go',
    'engine/access/rest/experimental/request/get_contracts.go',
    'engine/access/rest/experimental/request/get_scheduled_transactions.go',
    'engine/access/rest/experimental/routes/account_ft_transfers.go',
    'engine/access/rest/experimental/routes/account_nft_transfers.go',
    'engine/access/rest/experimental/routes/account_transactions.go',
    'engine/access/rest/experimental/routes/contracts.go',
    'engine/access/rest/experimental/routes/scheduled_transactions.go',
    'engine/access/rest/http/handler.go',
    'engine/access/rest/http/request/create_transaction.go',
    'engine/access/rest/http/request/get_account.go',
    'engine/access/rest/http/request/get_account_balance.go',
    'engine/access/rest/http/request/get_account_key.go',
    'engine/access/rest/http/request/get_account_keys.go',
    'engine/access/rest/http/request/get_block.go',
    'engine/access/rest/http/request/get_collection.go',
    'engine/access/rest/http/request/get_events.go',
    'engine/access/rest/http/request/get_execution_receipt.go',
    'engine/access/rest/http/request/get_execution_result.go',
    'engine/access/rest/http/request/get_script.go',
    'engine/access/rest/http/request/get_transaction.go',
    'engine/access/rest/http/request/height.go',
    'engine/access/rest/http/request/helpers.go',
    'engine/access/rest/http/request/script.go',
    'engine/access/rest/http/routes/account_balance.go',
    'engine/access/rest/http/routes/account_keys.go',
    'engine/access/rest/http/routes/accounts.go',
    'engine/access/rest/http/routes/blocks.go',
    'engine/access/rest/http/routes/collections.go',
    'engine/access/rest/http/routes/events.go',
    'engine/access/rest/http/routes/execution_receipts.go',
    'engine/access/rest/http/routes/execution_result.go',
    'engine/access/rest/http/routes/network.go',
    'engine/access/rest/http/routes/node_version_info.go',
    'engine/access/rest/http/routes/scripts.go',
    'engine/access/rest/http/routes/transactions.go',
    'engine/access/rest/router/router.go',
    'engine/access/rest/router/routes_experimental.go',
    'engine/access/rest/router/routes_main.go',
    'engine/access/rest/router/routes_ws_legacy.go',
    'engine/access/rest/server.go',
    'engine/access/rest/util/converter.go',
    'engine/access/rest/util/select_filter.go',
    'engine/access/rest/websockets/config.go',
    'engine/access/rest/websockets/connection.go',
    'engine/access/rest/websockets/connection_limited_handler.go',
    'engine/access/rest/websockets/controller.go',
    'engine/access/rest/websockets/data_providers/account_statuses_provider.go',
    'engine/access/rest/websockets/data_providers/args_validation.go',
    'engine/access/rest/websockets/data_providers/base_provider.go',
    'engine/access/rest/websockets/data_providers/block_digests_provider.go',
    'engine/access/rest/websockets/data_providers/block_headers_provider.go',
    'engine/access/rest/websockets/data_providers/blocks_provider.go',
    'engine/access/rest/websockets/data_providers/data_provider.go',
    'engine/access/rest/websockets/data_providers/events_provider.go',
    'engine/access/rest/websockets/data_providers/factory.go',
    'engine/access/rest/websockets/data_providers/send_and_get_transaction_statuses_provider.go',
    'engine/access/rest/websockets/data_providers/transaction_statuses_provider.go',
    'engine/access/rest/websockets/handler.go',
    'engine/access/rest/websockets/legacy/request/subscribe_events.go',
    'engine/access/rest/websockets/legacy/routes/subscribe_events.go',
    'engine/access/rest/websockets/legacy/websocket_handler.go',
    'engine/access/rest/websockets/subscription_id.go',
    'engine/access/rpc/backend/accounts/accounts.go',
    'engine/access/rpc/backend/accounts/provider/comparing.go',
    'engine/access/rpc/backend/accounts/provider/execution_node.go',
    'engine/access/rpc/backend/accounts/provider/failover.go',
    'engine/access/rpc/backend/accounts/provider/local.go',
    'engine/access/rpc/backend/accounts/provider/provider.go',
    'engine/access/rpc/backend/backend.go',
    'engine/access/rpc/backend/backend_block_base.go',
    'engine/access/rpc/backend/backend_block_details.go',
    'engine/access/rpc/backend/backend_block_headers.go',
    'engine/access/rpc/backend/backend_execution_results.go',
    'engine/access/rpc/backend/backend_network.go',
    'engine/access/rpc/backend/backend_stream_blocks.go',
    'engine/access/rpc/backend/common/consts.go',
    'engine/access/rpc/backend/common/errors.go',
    'engine/access/rpc/backend/common/height_error.go',
    'engine/access/rpc/backend/config.go',
    'engine/access/rpc/backend/events/events.go',
    'engine/access/rpc/backend/events/provider/execution_node.go',
    'engine/access/rpc/backend/events/provider/failover.go',
    'engine/access/rpc/backend/events/provider/local.go',
    'engine/access/rpc/backend/events/provider/provider.go',
    'engine/access/rpc/backend/node_communicator/communicator.go',
    'engine/access/rpc/backend/node_communicator/selector.go',
    'engine/access/rpc/backend/query_mode/mode.go',
    'engine/access/rpc/backend/script_executor.go',
    'engine/access/rpc/backend/transactions/cache.go',
    'engine/access/rpc/backend/transactions/error_messages/provider.go',
    'engine/access/rpc/backend/transactions/provider/execution_node.go',
    'engine/access/rpc/backend/transactions/provider/failover.go',
    'engine/access/rpc/backend/transactions/provider/local.go',
    'engine/access/rpc/backend/transactions/provider/provider.go',
    'engine/access/rpc/backend/transactions/status/deriver.go',
    'engine/access/rpc/backend/transactions/stream/stream_backend.go',
    'engine/access/rpc/backend/transactions/stream/transaction_metadata.go',
    'engine/access/rpc/backend/transactions/transactions.go',
    'engine/access/rpc/connection/cache.go',
    'engine/access/rpc/connection/connection.go',
    'engine/access/rpc/connection/manager.go',
    'engine/access/rpc/engine.go',
    'engine/access/rpc/engine_builder.go',
    'engine/access/rpc/handler.go',
    'engine/access/rpc/http_server.go',
    'engine/access/state_stream/account_status_filter.go',
    'engine/access/state_stream/backend/backend.go',
    'engine/access/state_stream/backend/backend_account_statuses.go',
    'engine/access/state_stream/backend/backend_events.go',
    'engine/access/state_stream/backend/backend_executiondata.go',
    'engine/access/state_stream/backend/engine.go',
    'engine/access/state_stream/backend/event_retriever.go',
    'engine/access/state_stream/backend/handler.go',
    'engine/access/state_stream/filter.go',
    'engine/access/state_stream/state_stream.go',
    'engine/access/subscription/streamer.go',
    'engine/access/subscription/subscribe_handler.go',
    'engine/access/subscription/subscription.go',
    'engine/access/subscription/tracker/base_tracker.go',
    'engine/access/subscription/tracker/block_tracker.go',
    'engine/access/subscription/tracker/execution_data_tracker.go',
    'engine/access/subscription/util.go',
    'engine/access/wrapper/access_api_client.go',
    'engine/access/wrapper/execution_api_client.go',
    'engine/collection/compliance.go',
    'engine/collection/compliance/core.go',
    'engine/collection/compliance/engine.go',
    'engine/collection/epochmgr/engine.go',
    'engine/collection/epochmgr/epoch_components.go',
    'engine/collection/epochmgr/factories/builder.go',
    'engine/collection/epochmgr/factories/cluster_state.go',
    'engine/collection/epochmgr/factories/compliance.go',
    'engine/collection/epochmgr/factories/epoch.go',
    'engine/collection/epochmgr/factories/hotstuff.go',
    'engine/collection/epochmgr/factories/hub.go',
    'engine/collection/epochmgr/factories/sync.go',
    'engine/collection/epochmgr/factories/sync_core.go',
    'engine/collection/epochmgr/factory.go',
    'engine/collection/events.go',
    'engine/collection/events/cluster_events_distributor.go',
    'engine/collection/events/distributor.go',
    'engine/collection/guaranteed_collection_publisher.go',
    'engine/collection/ingest/config.go',
    'engine/collection/ingest/engine.go',
    'engine/collection/ingest/rate_limiter.go',
    'engine/collection/message_hub/message_hub.go',
    'engine/collection/pusher/engine.go',
    'engine/collection/rpc/engine.go',
    'engine/collection/synchronization/engine.go',
    'engine/collection/synchronization/request_handler.go',
    'engine/consensus/approvals/aggregated_signatures.go',
    'engine/consensus/approvals/approval_collector.go',
    'engine/consensus/approvals/approvals_lru_cache.go',
    'engine/consensus/approvals/assignment_collector.go',
    'engine/consensus/approvals/assignment_collector_base.go',
    'engine/consensus/approvals/assignment_collector_statemachine.go',
    'engine/consensus/approvals/assignment_collector_tree.go',
    'engine/consensus/approvals/caches.go',
    'engine/consensus/approvals/caching_assignment_collector.go',
    'engine/consensus/approvals/chunk_collector.go',
    'engine/consensus/approvals/orphan_assignment_collector.go',
    'engine/consensus/approvals/request_tracker.go',
    'engine/consensus/approvals/signature_collector.go',
    'engine/consensus/approvals/tracker/record.go',
    'engine/consensus/approvals/tracker/tracker.go',
    'engine/consensus/approvals/verifying_assignment_collector.go',
    'engine/consensus/compliance.go',
    'engine/consensus/compliance/core.go',
    'engine/consensus/compliance/engine.go',
    'engine/consensus/dkg/doc.go',
    'engine/consensus/dkg/messaging_engine.go',
    'engine/consensus/dkg/reactor_engine.go',
    'engine/consensus/ingestion/core.go',
    'engine/consensus/ingestion/engine.go',
    'engine/consensus/matching.go',
    'engine/consensus/matching/core.go',
    'engine/consensus/matching/engine.go',
    'engine/consensus/message_hub/message_hub.go',
    'engine/consensus/sealing.go',
    'engine/consensus/sealing/core.go',
    'engine/consensus/sealing/engine.go',
    'engine/consensus/sealing_tracker.go',
    'engine/execution/block_result.go',
    'engine/execution/checker/core.go',
    'engine/execution/checker/engine.go',
    'engine/execution/collection_result.go',
    'engine/execution/computation/committer/committer.go',
    'engine/execution/computation/computer/computer.go',
    'engine/execution/computation/computer/result_collector.go',
    'engine/execution/computation/computer/transaction_coordinator.go',
    'engine/execution/computation/manager.go',
    'engine/execution/computation/query/executor.go',
    'engine/execution/computation/result/consumer.go',
    'engine/execution/computation/snapshot_provider.go',
    'engine/execution/engines.go',
    'engine/execution/ingestion/block_executed_notifier.go',
    'engine/execution/ingestion/block_queue/queue.go',
    'engine/execution/ingestion/core.go',
    'engine/execution/ingestion/fetcher.go',
    'engine/execution/ingestion/fetcher/access_fetcher.go',
    'engine/execution/ingestion/fetcher/fetcher.go',
    'engine/execution/ingestion/loader.go',
    'engine/execution/ingestion/loader/unexecuted_loader.go',
    'engine/execution/ingestion/loader/unfinalized_loader.go',
    'engine/execution/ingestion/machine.go',
    'engine/execution/ingestion/stop/stop_control.go',
    'engine/execution/ingestion/throttle.go',
    'engine/execution/ingestion/uploader/file_uploader.go',
    'engine/execution/ingestion/uploader/gcp_uploader.go',
    'engine/execution/ingestion/uploader/manager.go',
    'engine/execution/ingestion/uploader/model.go',
    'engine/execution/ingestion/uploader/retryable_uploader_wrapper.go',
    'engine/execution/ingestion/uploader/s3_uploader.go',
    'engine/execution/ingestion/uploader/uploader.go',
    'engine/execution/messages.go',
    'engine/execution/provider/engine.go',
    'engine/execution/pruner/config.go',
    'engine/execution/pruner/core.go',
    'engine/execution/pruner/engine.go',
    'engine/execution/pruner/executor.go',
    'engine/execution/pruner/prunable.go',
    'engine/execution/rpc/engine.go',
    'engine/execution/state/bootstrap/bootstrap.go',
    'engine/execution/state/state.go',
    'engine/execution/storehouse.go',
    'engine/execution/storehouse/background_indexer.go',
    'engine/execution/storehouse/background_indexer_engine.go',
    'engine/execution/storehouse/background_indexer_factory.go',
    'engine/execution/storehouse/background_indexer_provider.go',
    'engine/execution/storehouse/block_end_snapshot.go',
    'engine/execution/storehouse/checkpoint_validator.go',
    'engine/execution/storehouse/executing_block_snapshot.go',
    'engine/execution/storehouse/in_memory_register_store.go',
    'engine/execution/storehouse/register_engine.go',
    'engine/execution/storehouse/register_store.go',
    'engine/execution/storehouse/register_store_metrics.go',
    'engine/execution/utils/hasher.go',
    'engine/protocol/api.go',
    'engine/protocol/handler.go',
    'engine/verification/assigner/blockconsumer/consumer.go',
    'engine/verification/assigner/blockconsumer/worker.go',
    'engine/verification/assigner/engine.go',
    'engine/verification/assigner/processor.go',
    'engine/verification/fetcher/chunkconsumer/consumer.go',
    'engine/verification/fetcher/chunkconsumer/job.go',
    'engine/verification/fetcher/chunkconsumer/jobs.go',
    'engine/verification/fetcher/chunkconsumer/worker.go',
    'engine/verification/fetcher/engine.go',
    'engine/verification/fetcher/errors.go',
    'engine/verification/fetcher/processor.go',
    'engine/verification/fetcher/requester.go',
    'engine/verification/requester/qualifier.go',
    'engine/verification/requester/requester.go',
    'engine/verification/utils/hasher.go',
    'engine/verification/verifier/engine.go',
    'engine/verification/verifier/verifiers.go',
    'fvm/blueprints/bridge.go',
    'fvm/blueprints/contracts.go',
    'fvm/blueprints/epochs.go',
    'fvm/blueprints/fees.go',
    'fvm/blueprints/scheduled_callback.go',
    'fvm/blueprints/source_of_randomness.go',
    'fvm/blueprints/system.go',
    'fvm/blueprints/token.go',
    'fvm/blueprints/version_beacon.go',
    'fvm/bootstrap.go',
    'fvm/context.go',
    'fvm/crypto/crypto.go',
    'fvm/crypto/hash.go',
    'fvm/environment/account-key-metadata/digest.go',
    'fvm/environment/account-key-metadata/encoder_util.go',
    'fvm/environment/account-key-metadata/key_index_mapping_group.go',
    'fvm/environment/account-key-metadata/metadata.go',
    'fvm/environment/account-key-metadata/weight_and_revoked_group.go',
    'fvm/environment/account_creator.go',
    'fvm/environment/account_info.go',
    'fvm/environment/account_key_reader.go',
    'fvm/environment/account_key_updater.go',
    'fvm/environment/account_local_id_generator.go',
    'fvm/environment/account_public_key_util.go',
    'fvm/environment/accounts.go',
    'fvm/environment/accounts_status.go',
    'fvm/environment/block_info.go',
    'fvm/environment/blocks.go',
    'fvm/environment/contract_reader.go',
    'fvm/environment/contract_updater.go',
    'fvm/environment/crypto_library.go',
    'fvm/environment/derived_data_invalidator.go',
    'fvm/environment/env.go',
    'fvm/environment/event_emitter.go',
    'fvm/environment/event_encoder.go',
    'fvm/environment/evm_block_hash_list.go',
    'fvm/environment/evm_block_store.go',
    'fvm/environment/facade_env.go',
    'fvm/environment/history_random_source_provider.go',
    'fvm/environment/invoker.go',
    'fvm/environment/meter.go',
    'fvm/environment/minimum_required_version.go',
    'fvm/environment/parse_restricted_checker.go',
    'fvm/environment/program_logger.go',
    'fvm/environment/program_recovery.go',
    'fvm/environment/programs.go',
    'fvm/environment/random_generator.go',
    'fvm/environment/runtime.go',
    'fvm/environment/script_info.go',
    'fvm/environment/system_contracts.go',
    'fvm/environment/tracer.go',
    'fvm/environment/transaction_info.go',
    'fvm/environment/uuids.go',
    'fvm/environment/value_store.go',
    'fvm/errors/account_key.go',
    'fvm/errors/accounts.go',
    'fvm/errors/base.go',
    'fvm/errors/codes.go',
    'fvm/errors/contracts.go',
    'fvm/errors/errors.go',
    'fvm/errors/errors_collector.go',
    'fvm/errors/execution.go',
    'fvm/errors/failures.go',
    'fvm/errors/txVerifier.go',
    'fvm/evm/backends/backend.go',
    'fvm/evm/backends/wrappedEnv.go',
    'fvm/evm/emulator/config.go',
    'fvm/evm/emulator/emulator.go',
    'fvm/evm/emulator/signer.go',
    'fvm/evm/emulator/state/account.go',
    'fvm/evm/emulator/state/base.go',
    'fvm/evm/emulator/state/code.go',
    'fvm/evm/emulator/state/collection.go',
    'fvm/evm/emulator/state/delta.go',
    'fvm/evm/emulator/state/diff.go',
    'fvm/evm/emulator/state/exporter.go',
    'fvm/evm/emulator/state/extract.go',
    'fvm/evm/emulator/state/importer.go',
    'fvm/evm/emulator/state/stateDB.go',
    'fvm/evm/emulator/state/updateCommitter.go',
    'fvm/evm/emulator/tracker.go',
    'fvm/evm/events/events.go',
    'fvm/evm/events/utils.go',
    'fvm/evm/evm.go',
    'fvm/evm/handler/addressAllocator.go',
    'fvm/evm/handler/coa/coa.go',
    'fvm/evm/handler/handler.go',
    'fvm/evm/handler/precompiles.go',
    'fvm/evm/impl/abi.go',
    'fvm/evm/impl/impl.go',
    'fvm/evm/offchain/blocks/block_context.go',
    'fvm/evm/offchain/blocks/block_proposal.go',
    'fvm/evm/offchain/blocks/blocks.go',
    'fvm/evm/offchain/blocks/meta.go',
    'fvm/evm/offchain/blocks/provider.go',
    'fvm/evm/offchain/query/view.go',
    'fvm/evm/offchain/query/viewProvider.go',
    'fvm/evm/offchain/storage/ephemeral.go',
    'fvm/evm/offchain/storage/readonly.go',
    'fvm/evm/offchain/sync/replay.go',
    'fvm/evm/offchain/sync/replayer.go',
    'fvm/evm/offchain/utils/collection.go',
    'fvm/evm/offchain/utils/replay.go',
    'fvm/evm/offchain/utils/types.go',
    'fvm/evm/offchain/utils/verify.go',
    'fvm/evm/precompiles/abi.go',
    'fvm/evm/precompiles/arch.go',
    'fvm/evm/precompiles/precompile.go',
    'fvm/evm/precompiles/replayer.go',
    'fvm/evm/precompiles/selector.go',
    'fvm/evm/stdlib/checking.go',
    'fvm/evm/stdlib/contract.go',
    'fvm/evm/stdlib/type.go',
    'fvm/evm/types/account.go',
    'fvm/evm/types/address.go',
    'fvm/evm/types/balance.go',
    'fvm/evm/types/block.go',
    'fvm/evm/types/call.go',
    'fvm/evm/types/chainIDs.go',
    'fvm/evm/types/codeFinder.go',
    'fvm/evm/types/emulator.go',
    'fvm/evm/types/errors.go',
    'fvm/evm/types/handler.go',
    'fvm/evm/types/offchain.go',
    'fvm/evm/types/precompiled.go',
    'fvm/evm/types/proof.go',
    'fvm/evm/types/result.go',
    'fvm/evm/types/state.go',
    'fvm/evm/types/tokenVault.go',
    'fvm/executionParameters.go',
    'fvm/fvm.go',
    'fvm/initialize/options.go',
    'fvm/inspection/inspector.go',
    'fvm/inspection/token_changes.go',
    'fvm/meter/computation_meter.go',
    'fvm/meter/event_meter.go',
    'fvm/meter/interaction_meter.go',
    'fvm/meter/memory_meter.go',
    'fvm/meter/meter.go',
    'fvm/migration/migration.go',
    'fvm/runtime/cadence_function_declarations.go',
    'fvm/runtime/reusable_cadence_runtime.go',
    'fvm/runtime/reusable_cadence_runtime_pool.go',
    'fvm/runtime/wrapped_cadence_runtime.go',
    'fvm/script.go',
    'fvm/storage/block_database.go',
    'fvm/storage/derived/dependencies.go',
    'fvm/storage/derived/derived_block_data.go',
    'fvm/storage/derived/derived_chain_data.go',
    'fvm/storage/derived/invalidator.go',
    'fvm/storage/derived/table.go',
    'fvm/storage/derived/table_invalidator.go',
    'fvm/storage/errors/errors.go',
    'fvm/storage/logical/time.go',
    'fvm/storage/primary/block_data.go',
    'fvm/storage/primary/intersect.go',
    'fvm/storage/primary/snapshot_tree.go',
    'fvm/storage/snapshot/execution_snapshot.go',
    'fvm/storage/snapshot/snapshot_tree.go',
    'fvm/storage/snapshot/storage_snapshot.go',
    'fvm/storage/state/execution_state.go',
    'fvm/storage/state/spock_state.go',
    'fvm/storage/state/storage_state.go',
    'fvm/storage/state/transaction_state.go',
    'fvm/storage/transaction.go',
    'fvm/systemcontracts/system_contracts.go',
    'fvm/transaction.go',
    'fvm/transactionInvoker.go',
    'fvm/transactionPayerBalanceChecker.go',
    'fvm/transactionSequenceNum.go',
    'fvm/transactionStorageLimiter.go',
    'fvm/transactionVerifier.go',
    'ledger/common/bitutils/utils.go',
    'ledger/common/convert/convert.go',
    'ledger/common/hash/copy_generic.go',
    'ledger/common/hash/copy_unaligned.go',
    'ledger/common/hash/hash.go',
    'ledger/common/hash/keccak.go',
    'ledger/common/hash/keccakf.go',
    'ledger/common/hash/sha3.go',
    'ledger/common/pathfinder/pathfinder.go',
    'ledger/common/proof/proof.go',
    'ledger/common/utils/utils.go',
    'ledger/complete/compactor.go',
    'ledger/complete/factory.go',
    'ledger/complete/ledger.go',
    'ledger/complete/ledger_stats.go',
    'ledger/complete/ledger_with_compactor.go',
    'ledger/complete/mtrie/flattener/encoding.go',
    'ledger/complete/mtrie/flattener/encoding_v3.go',
    'ledger/complete/mtrie/flattener/encoding_v4.go',
    'ledger/complete/mtrie/flattener/iterator.go',
    'ledger/complete/mtrie/forest.go',
    'ledger/complete/mtrie/node/node.go',
    'ledger/complete/mtrie/trie/trie.go',
    'ledger/complete/mtrie/trieCache.go',
    'ledger/complete/wal/checkpoint_v6_leaf_reader.go',
    'ledger/complete/wal/checkpoint_v6_reader.go',
    'ledger/complete/wal/checkpoint_v6_writer.go',
    'ledger/complete/wal/checkpointer.go',
    'ledger/complete/wal/checksum.go',
    'ledger/complete/wal/encoding.go',
    'ledger/complete/wal/fadvise.go',
    'ledger/complete/wal/fadvise_linux.go',
    'ledger/complete/wal/syncrename.go',
    'ledger/complete/wal/triequeue.go',
    'ledger/complete/wal/wal.go',
    'ledger/config.go',
    'ledger/errors.go',
    'ledger/factory.go',
    'ledger/factory/factory.go',
    'ledger/ledger.go',
    'ledger/partial/ledger.go',
    'ledger/partial/ptrie/errors.go',
    'ledger/partial/ptrie/node.go',
    'ledger/partial/ptrie/partialTrie.go',
    'ledger/remote/client.go',
    'ledger/remote/encoding.go',
    'ledger/remote/factory.go',
    'ledger/remote/service.go',
    'ledger/trie.go',
    'ledger/trie_encoder.go',
    'model/chainsync/range.go',
    'model/chainsync/status.go',
    'model/chunks/chunkFaults.go',
    'model/chunks/chunkLocator.go',
    'model/chunks/chunkassignment.go',
    'model/chunks/chunks.go',
    'model/chunks/executionDataFaults.go',
    'model/cluster/block.go',
    'model/cluster/payload.go',
    'model/dkg/dkg.go',
    'model/events/parse.go',
    'model/flow/account.go',
    'model/flow/account_encoder.go',
    'model/flow/address.go',
    'model/flow/aggregated_signature.go',
    'model/flow/assignment/sort.go',
    'model/flow/block.go',
    'model/flow/chain.go',
    'model/flow/chunk.go',
    'model/flow/cluster.go',
    'model/flow/collection.go',
    'model/flow/collectionGuarantee.go',
    'model/flow/constants.go',
    'model/flow/dkg.go',
    'model/flow/entity.go',
    'model/flow/epoch.go',
    'model/flow/event.go',
    'model/flow/execution_receipt.go',
    'model/flow/execution_result.go',
    'model/flow/factory/cluster_list.go',
    'model/flow/filter/id/identifier.go',
    'model/flow/filter/identity.go',
    'model/flow/header.go',
    'model/flow/header_body_builder.go',
    'model/flow/identifier.go',
    'model/flow/identifierList.go',
    'model/flow/identifier_order.go',
    'model/flow/identity.go',
    'model/flow/identity_list.go',
    'model/flow/identity_order.go',
    'model/flow/incorporated_result.go',
    'model/flow/incorporated_result_seal.go',
    'model/flow/index.go',
    'model/flow/kvstore.go',
    'model/flow/ledger.go',
    'model/flow/mapfunc/identity.go',
    'model/flow/payload.go',
    'model/flow/protocol_state.go',
    'model/flow/quorum_certificate.go',
    'model/flow/resultApproval.go',
    'model/flow/role.go',
    'model/flow/schemes.go',
    'model/flow/seal.go',
    'model/flow/sealing_segment.go',
    'model/flow/service_event.go',
    'model/flow/slashable.go',
    'model/flow/synchronization.go',
    'model/flow/timeout_certificate.go',
    'model/flow/transaction.go',
    'model/flow/transaction_body_builder.go',
    'model/flow/transaction_result.go',
    'model/flow/transaction_timing.go',
    'model/flow/version_beacon.go',
    'model/flow/webauthn.go',
    'model/messages/collection.go',
    'model/messages/consensus.go',
    'model/messages/dkg.go',
    'model/messages/exchange.go',
    'model/messages/execution.go',
    'model/messages/synchronization.go',
    'model/messages/untrusted_message.go',
    'model/messages/verification.go',
    'model/verification/chunkDataPackRequest.go',
    'model/verification/chunkDataPackResponse.go',
    'model/verification/chunkStatus.go',
    'model/verification/convert/convert.go',
    'model/verification/verifiableChunkData.go',
    'module/chainsync/core.go',
    'module/compliance/config.go',
    'module/dkg/broker.go',
    'module/dkg/client.go',
    'module/dkg/controller.go',
    'module/dkg/controller_factory.go',
    'module/dkg/doc.go',
    'module/dkg/errors.go',
    'module/dkg/hasher.go',
    'module/dkg/instance.go',
    'module/dkg/recovery.go',
    'module/dkg/state.go',
    'module/dkg/tunnel.go',
    'module/dkg/verification.go',
    'module/epochs/base_client.go',
    'module/epochs/epoch_config.go',
    'module/epochs/epoch_lookup.go',
    'module/epochs/errors.go',
    'module/epochs/machine_account.go',
    'module/epochs/qc_client.go',
    'module/epochs/qc_voter.go',
    'module/execution/registers_async.go',
    'module/execution/scripts.go',
    'module/finalizer/collection/finalizer.go',
    'module/finalizer/consensus/cleanup.go',
    'module/finalizer/consensus/finalizer.go',
    'module/finalizer/consensus/options.go',
    'module/mempool/assignments.go',
    'module/mempool/backData.go',
    'module/mempool/chunk_requests.go',
    'module/mempool/chunk_statuses.go',
    'module/mempool/common.go',
    'module/mempool/consensus/exec_fork_actor.go',
    'module/mempool/consensus/exec_fork_suppressor.go',
    'module/mempool/consensus/execution_tree.go',
    'module/mempool/consensus/incorporated_result_seals.go',
    'module/mempool/consensus/receipt_equivalence_class.go',
    'module/mempool/dns_cache.go',
    'module/mempool/entity/executableblock.go',
    'module/mempool/epochs/transactions.go',
    'module/mempool/errors.go',
    'module/mempool/execution_data.go',
    'module/mempool/execution_tree.go',
    'module/mempool/guarantees.go',
    'module/mempool/identifier_map.go',
    'module/mempool/incorporated_result_seals.go',
    'module/mempool/mempool.go',
    'module/mempool/mutable_back_data.go',
    'module/mempool/pending_receipts.go',
    'module/mempool/transaction_timings.go',
    'module/mempool/transactions.go',
    'module/signature/aggregation.go',
    'module/signature/checksum.go',
    'module/signature/errors.go',
    'module/signature/signer_indices.go',
    'module/signature/signing_tags.go',
    'module/signature/threshold.go',
    'module/signature/type_encoder.go',
    'module/state_synchronization/execution_data_requester.go',
    'module/state_synchronization/index_reporter.go',
    'module/state_synchronization/indexer/collection_executed_metric.go',
    'module/state_synchronization/indexer/in_memory_indexer.go',
    'module/state_synchronization/indexer/indexer.go',
    'module/state_synchronization/indexer/indexer_core.go',
    'module/state_synchronization/indexer/ledger_trie_updates_test_utils.go',
    'module/state_synchronization/indexer/util.go',
    'module/state_synchronization/requester/distributer.go',
    'module/state_synchronization/requester/execution_data_requester.go',
    'module/state_synchronization/requester/jobs/execution_data_reader.go',
    'module/state_synchronization/requester/jobs/jobs.go',
    'module/state_synchronization/requester/oneshot_execution_data_requester.go',
    'module/validation/common.go',
    'module/validation/receipt_validator.go',
    'module/validation/seal_validator.go',
    'network/alsp.go',
    'network/alsp/cache.go',
    'network/alsp/internal/cache.go',
    'network/alsp/internal/reported_misbehavior_work.go',
    'network/alsp/manager/manager.go',
    'network/alsp/misbehavior.go',
    'network/alsp/model/params.go',
    'network/alsp/model/record.go',
    'network/alsp/report.go',
    'network/blob_service.go',
    'network/channels/channel.go',
    'network/channels/channels.go',
    'network/channels/errors.go',
    'network/channels/topic.go',
    'network/codec.go',
    'network/codec/cbor/codec.go',
    'network/codec/cbor/decoder.go',
    'network/codec/cbor/encoder.go',
    'network/codec/codes.go',
    'network/codec/errors.go',
    'network/compressor.go',
    'network/compressor/gzipCompressor.go',
    'network/compressor/lz4Compressor.go',
    'network/conduit.go',
    'network/converter/network.go',
    'network/disallow.go',
    'network/engine.go',
    'network/errors.go',
    'network/internal/p2pfixtures/fixtures.go',
    'network/internal/p2putils/utils.go',
    'network/message/authorization.go',
    'network/message/errors.go',
    'network/message/gossipsub.go',
    'network/message/init.go',
    'network/message/message_scope.go',
    'network/message/protocols.go',
    'network/message_scope.go',
    'network/netconf/config.go',
    'network/netconf/connection_manager.go',
    'network/netconf/flags.go',
    'network/netconf/unicast.go',
    'network/network.go',
    'network/p2p/blob/blob_service.go',
    'network/p2p/builder.go',
    'network/p2p/builder/config/config.go',
    'network/p2p/builder/gossipsub/gossipSubBuilder.go',
    'network/p2p/builder/libp2pNodeBuilder.go',
    'network/p2p/builder/libp2pscaler.go',
    'network/p2p/builder/resourceLimit.go',
    'network/p2p/builder/utils.go',
    'network/p2p/cache.go',
    'network/p2p/cache/gossipsub_spam_records.go',
    'network/p2p/cache/node_disallow_list_wrapper.go',
    'network/p2p/cache/protocol_state_provider.go',
    'network/p2p/conduit/conduit.go',
    'network/p2p/config/errors.go',
    'network/p2p/config/gossipsub.go',
    'network/p2p/config/gossipsub_rpc_inspectors.go',
    'network/p2p/config/peer_scoring.go',
    'network/p2p/config/score_registry.go',
    'network/p2p/connection/connManager.go',
    'network/p2p/connection/connection_gater.go',
    'network/p2p/connection/connector.go',
    'network/p2p/connection/connector_factory.go',
    'network/p2p/connection/connector_host.go',
    'network/p2p/connection/internal/loggerNotifiee.go',
    'network/p2p/connection/internal/relayNotifee.go',
    'network/p2p/connection/peerManager.go',
    'network/p2p/connectionGater.go',
    'network/p2p/connector.go',
    'network/p2p/consumers.go',
    'network/p2p/dht/dht.go',
    'network/p2p/disallowListCache.go',
    'network/p2p/dns/cache.go',
    'network/p2p/dns/resolver.go',
    'network/p2p/id_translator.go',
    'network/p2p/inspector/internal/cache/cache.go',
    'network/p2p/inspector/internal/cache/cluster_prefixed_received_tracker.go',
    'network/p2p/inspector/internal/cache/record.go',
    'network/p2p/inspector/internal/ratelimit/control_message_rate_limiter.go',
    'network/p2p/inspector/internal/utils.go',
    'network/p2p/inspector/validation/control_message_validation_inspector.go',
    'network/p2p/inspector/validation/errors.go',
    'network/p2p/inspector/validation/inspect_message_request.go',
    'network/p2p/inspector/validation/utils.go',
    'network/p2p/keyutils/keyTranslator.go',
    'network/p2p/libp2pNode.go',
    'network/p2p/logging/internal/peerIdCache.go',
    'network/p2p/message/types.go',
    'network/p2p/node/gossipSubAdapter.go',
    'network/p2p/node/gossipSubAdapterConfig.go',
    'network/p2p/node/gossipSubTopic.go',
    'network/p2p/node/internal/cache.go',
    'network/p2p/node/internal/protocolPeerCache.go',
    'network/p2p/node/libp2pNode.go',
    'network/p2p/peerManager.go',
    'network/p2p/ping/ping.go',
    'network/p2p/pubsub.go',
    'network/p2p/rate_limiter.go',
    'network/p2p/stream.go',
    'network/p2p/subscription.go',
    'network/p2p/subscription/subscriptionManager.go',
    'network/p2p/subscription/subscription_filter.go',
    'network/p2p/tracer/gossipSubMeshTracer.go',
    'network/p2p/tracer/gossipSubNoopTracer.go',
    'network/p2p/tracer/gossipSubScoreTracer.go',
    'network/p2p/tracer/internal/duplicate_msgs_counter_cache.go',
    'network/p2p/tracer/internal/duplicate_msgs_counter_entity.go',
    'network/p2p/tracer/internal/rpc_sent_cache.go',
    'network/p2p/tracer/internal/rpc_sent_tracker.go',
    'network/p2p/translator/fixed_translator.go',
    'network/p2p/translator/hierarchical_translator.go',
    'network/p2p/translator/identity_provider_translator.go',
    'network/p2p/translator/unstaked_translator.go',
    'network/p2p/unicast/cache/unicastConfigCache.go',
    'network/p2p/unicast/dialConfig.go',
    'network/p2p/unicast/dialConfigCache.go',
    'network/p2p/unicast/errors.go',
    'network/p2p/unicast/manager.go',
    'network/p2p/unicast/manager_config.go',
    'network/p2p/unicast/protocols/gzip.go',
    'network/p2p/unicast/protocols/internal/compressedStream.go',
    'network/p2p/unicast/protocols/protocol.go',
    'network/p2p/unicast/ratelimit/bandwidth_rate_limiter.go',
    'network/p2p/unicast/ratelimit/distributor.go',
    'network/p2p/unicast/ratelimit/noop_rate_limiter.go',
    'network/p2p/unicast/ratelimit/rate_limiters.go',
    'network/p2p/unicast/stream/errors.go',
    'network/p2p/unicast/stream/factory.go',
    'network/p2p/unicast/stream/plain.go',
    'network/p2p/unicast_manager.go',
    'network/p2p/utils/p2putils.go',
    'network/p2p/utils/ratelimiter/internal/rate_limiter_map.go',
    'network/p2p/utils/ratelimiter/rate_limiter.go',
    'network/ping.go',
    'network/proxy/conduit.go',
    'network/proxy/network.go',
    'network/queue.go',
    'network/resolver.go',
    'network/subscription.go',
    'network/topology.go',
    'network/underlay/internal/readSubscription.go',
    'network/underlay/network.go',
    'network/validator.go',
    'network/validator/any_validator.go',
    'network/validator/authorized_sender_validator.go',
    'network/validator/not_validator.go',
    'network/validator/origin_validator.go',
    'network/validator/pubsub/topic_validator.go',
    'network/validator/sender_validator.go',
    'network/validator/target_validator.go',
    'network/validator/validator.go',
    'network/violations_consumer.go',
    'state/protocol/badger/mutator.go',
    'state/protocol/badger/snapshot.go',
    'state/protocol/badger/state.go',
    'state/protocol/blocktimer.go',
    'state/protocol/blocktimer/blocktimer.go',
    'state/protocol/chain_state.go',
    'state/protocol/cluster.go',
    'state/protocol/datastore/params.go',
    'state/protocol/datastore/validity.go',
    'state/protocol/defaults.go',
    'state/protocol/dkg.go',
    'state/protocol/epoch.go',
    'state/protocol/errors.go',
    'state/protocol/events.go',
    'state/protocol/events/distributor.go',
    'state/protocol/events/gadgets.go',
    'state/protocol/events/gadgets/heights.go',
    'state/protocol/events/gadgets/identity_deltas.go',
    'state/protocol/events/gadgets/views.go',
    'state/protocol/execution.go',
    'state/protocol/inmem/cluster.go',
    'state/protocol/inmem/convert.go',
    'state/protocol/inmem/dkg.go',
    'state/protocol/inmem/encodable.go',
    'state/protocol/inmem/epoch.go',
    'state/protocol/inmem/epoch_protocol_state.go',
    'state/protocol/inmem/params.go',
    'state/protocol/inmem/snapshot.go',
    'state/protocol/invalid/params.go',
    'state/protocol/invalid/snapshot.go',
    'state/protocol/kvstore.go',
    'state/protocol/params.go',
    'state/protocol/prg/customizers.go',
    'state/protocol/prg/prg.go',
    'state/protocol/protocol_state.go',
    'state/protocol/protocol_state/common/base_statemachine.go',
    'state/protocol/protocol_state/consumer.go',
    'state/protocol/protocol_state/epochs/base_statemachine.go',
    'state/protocol/protocol_state/epochs/factory.go',
    'state/protocol/protocol_state/epochs/fallback_statemachine.go',
    'state/protocol/protocol_state/epochs/happy_path_statemachine.go',
    'state/protocol/protocol_state/epochs/identity_ejector.go',
    'state/protocol/protocol_state/epochs/statemachine.go',
    'state/protocol/protocol_state/kvstore.go',
    'state/protocol/protocol_state/kvstore/encoding.go',
    'state/protocol/protocol_state/kvstore/errors.go',
    'state/protocol/protocol_state/kvstore/factory.go',
    'state/protocol/protocol_state/kvstore/kvstore_storage.go',
    'state/protocol/protocol_state/kvstore/models.go',
    'state/protocol/protocol_state/kvstore/set_value_statemachine.go',
    'state/protocol/protocol_state/kvstore/upgrade_statemachine.go',
    'state/protocol/protocol_state/kvstore_storage.go',
    'state/protocol/protocol_state/state/protocol_state.go',
    'state/protocol/snapshot.go',
    'state/protocol/util.go',
    'state/protocol/validity.go',
    'storage/account_transactions.go',
    'storage/account_transfers.go',
    'storage/all.go',
    'storage/approvals.go',
    'storage/badger/all.go',
    'storage/badger/batch.go',
    'storage/badger/cache.go',
    'storage/badger/cleaner.go',
    'storage/badger/dkg_state.go',
    'storage/badger/init.go',
    'storage/badger/operation/common.go',
    'storage/badger/operation/dkg.go',
    'storage/badger/operation/init.go',
    'storage/badger/operation/max.go',
    'storage/badger/operation/modifiers.go',
    'storage/badger/operation/prefix.go',
    'storage/badger/transaction/tx.go',
    'storage/batch.go',
    'storage/blocks.go',
    'storage/chunk_data_packs.go',
    'storage/chunk_data_packs_stored.go',
    'storage/chunks_queue.go',
    'storage/cluster_blocks.go',
    'storage/cluster_payloads.go',
    'storage/collections.go',
    'storage/commits.go',
    'storage/computation_result.go',
    'storage/consumer_progress.go',
    'storage/contract_deployments.go',
    'storage/deferred/operations.go',
    'storage/dkg.go',
    'storage/epoch_commits.go',
    'storage/epoch_protocol_state.go',
    'storage/epoch_setups.go',
    'storage/errors.go',
    'storage/events.go',
    'storage/execution_fork_evidence.go',
    'storage/guarantees.go',
    'storage/headers.go',
    'storage/height.go',
    'storage/index.go',
    'storage/index_iterator.go',
    'storage/inmemory/collections_reader.go',
    'storage/inmemory/events_reader.go',
    'storage/inmemory/light_transaction_results_reader.go',
    'storage/inmemory/registers_reader.go',
    'storage/inmemory/transaction_result_error_messages_reader.go',
    'storage/inmemory/transactions_reader.go',
    'storage/latest_persisted_sealed_result.go',
    'storage/ledger.go',
    'storage/light_transaction_results.go',
    'storage/locks.go',
    'storage/merkle/errors.go',
    'storage/merkle/node.go',
    'storage/merkle/proof.go',
    'storage/merkle/tree.go',
    'storage/migration/migration.go',
    'storage/migration/runner.go',
    'storage/migration/sstables.go',
    'storage/migration/validation.go',
    'storage/node_disallow_list.go',
    'storage/operation/approvals.go',
    'storage/operation/badgerimpl/dbstore.go',
    'storage/operation/badgerimpl/iterator.go',
    'storage/operation/badgerimpl/reader.go',
    'storage/operation/badgerimpl/seeker.go',
    'storage/operation/badgerimpl/writer.go',
    'storage/operation/callbacks.go',
    'storage/operation/children.go',
    'storage/operation/chunk_data_packs.go',
    'storage/operation/chunk_locators.go',
    'storage/operation/cluster.go',
    'storage/operation/codec.go',
    'storage/operation/collections.go',
    'storage/operation/commits.go',
    'storage/operation/computation_result.go',
    'storage/operation/consume_progress.go',
    'storage/operation/epoch.go',
    'storage/operation/epoch_protocol_state.go',
    'storage/operation/events.go',
    'storage/operation/executed.go',
    'storage/operation/execution_fork_evidence.go',
    'storage/operation/guarantees.go',
    'storage/operation/headers.go',
    'storage/operation/heights.go',
    'storage/operation/index.go',
    'storage/operation/instance_params.go',
    'storage/operation/interactions.go',
    'storage/operation/jobs.go',
    'storage/operation/multi_dbstore.go',
    'storage/operation/multi_iterator.go',
    'storage/operation/multi_reader.go',
    'storage/operation/multi_seeker.go',
    'storage/operation/node_disallow_list.go',
    'storage/operation/payload.go',
    'storage/operation/pebbleimpl/dbstore.go',
    'storage/operation/pebbleimpl/iterator.go',
    'storage/operation/pebbleimpl/reader.go',
    'storage/operation/pebbleimpl/seeker.go',
    'storage/operation/pebbleimpl/writer.go',
    'storage/operation/prefix.go',
    'storage/operation/proposal_signatures.go',
    'storage/operation/protocol_kv_store.go',
    'storage/operation/qcs.go',
    'storage/operation/reads.go',
    'storage/operation/reads_functors.go',
    'storage/operation/receipts.go',
    'storage/operation/results.go',
    'storage/operation/scheduled_transactions.go',
    'storage/operation/stats.go',
    'storage/operation/transaction_results.go',
    'storage/operation/transactions.go',
    'storage/operation/version_beacon.go',
    'storage/operation/views.go',
    'storage/operation/writes.go',
    'storage/operation/writes_functors.go',
    'storage/operations.go',
    'storage/payloads.go',
    'storage/pebble/batch.go',
    'storage/pebble/bootstrap.go',
    'storage/pebble/cache.go',
    'storage/pebble/config.go',
    'storage/pebble/constants.go',
    'storage/pebble/lookup.go',
    'storage/pebble/open.go',
    'storage/pebble/registers.go',
    'storage/pebble/registers/comparer.go',
    'storage/pebble/registers_cache.go',
    'storage/protocol_kv_store.go',
    'storage/qcs.go',
    'storage/receipts.go',
    'storage/registers.go',
    'storage/results.go',
    'storage/scheduled_transactions.go',
    'storage/scheduled_transactions_index.go',
    'storage/seals.go',
    'storage/store/approvals.go',
    'storage/store/blocks.go',
    'storage/store/cache.go',
    'storage/store/chunk_data_packs.go',
    'storage/store/chunk_data_packs_stored.go',
    'storage/store/chunks_queue.go',
    'storage/store/cluster_blocks.go',
    'storage/store/cluster_payloads.go',
    'storage/store/collections.go',
    'storage/store/commits.go',
    'storage/store/computation_result.go',
    'storage/store/consumer_progress.go',
    'storage/store/epoch_commits.go',
    'storage/store/epoch_protocol_state.go',
    'storage/store/epoch_setups.go',
    'storage/store/events.go',
    'storage/store/execution_fork_evidence.go',
    'storage/store/group_cache.go',
    'storage/store/guarantees.go',
    'storage/store/headers.go',
    'storage/store/index.go',
    'storage/store/init.go',
    'storage/store/latest_persisted_sealed_result.go',
    'storage/store/light_transaction_results.go',
    'storage/store/my_receipts.go',
    'storage/store/node_disallow_list.go',
    'storage/store/payloads.go',
    'storage/store/proposal_signatures.go',
    'storage/store/protocol_kv_store.go',
    'storage/store/qcs.go',
    'storage/store/receipts.go',
    'storage/store/results.go',
    'storage/store/scheduled_transactions.go',
    'storage/store/seals.go',
    'storage/store/transaction_result_error_messages.go',
    'storage/store/transaction_results.go',
    'storage/store/transactions.go',
    'storage/store/version_beacon.go',
    'storage/transaction_result_error_messages.go',
    'storage/transaction_results.go',
    'storage/transactions.go',
    'storage/version_beacon.go',
]
target_scopes = [
    'Critical. Execution-layer vulnerability causing unauthorized account manipulation or unauthorized mutation of another user account state',
    'Critical. Execution-layer vulnerability circumventing Cadence/FVM/EVM resource semantics, including unauthorized resource construction, duplication, destruction, or use-after-destruction',
    'Critical. Runtime or execution-environment vulnerability enabling unauthorized access to private data, node secrets, randomness state, or sandboxed host capabilities through attacker-controlled transactions or scripts',
    'Critical. Protocol-layer vulnerability originating from an unstaked Access or Observer node that alters existing data in execution-node or consensus-node databases',
    'Critical. Flow EVM or cross-VM asset handling vulnerability causing theft, loss, permanent lock, escrow mis-accounting, entitlement bypass, or resource duplication for standards-compliant bridged assets',
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit and fuzzing questions for one Flow target.

    target_file format:
    "'File Name: fvm/transactionVerifier.go -> Scope: Critical. Execution-layer vulnerability causing unauthorized account manipulation or unauthorized mutation of another user account state'"
    """

    prompt = f"""
    ```

    Generate exploit-focused security audit and fuzzing questions for this exact Flow protocol target:

    {target_file}

    Use live context from the project if available: HotStuff/Jolteon consensus, collection guarantees, execution receipts/results, sealing and approvals, dynamic protocol state, epochs/DKG/random beacon, FVM/Cadence/EVM execution, transaction validation, ledger/storage, state synchronization, networking, ALSP, peer scoring, message validation, and access validation.

    Protocol focus:
    Flow is a multi-role BFT blockchain protocol. Consensus nodes finalize blocks with HotStuff/Jolteon, collection nodes build signed collections, execution nodes execute blocks and publish execution receipts, verification nodes approve execution chunks, and access nodes expose transaction and query entrypoints. The audit target is production Flow protocol and node behavior only.

    Core invariants:

    * Attacker-signed transactions, scripts, contracts, or EVM calls must never manipulate another account or bypass payer/proposer/key authorization.
    * Cadence/FVM/EVM execution must never permit unauthorized resource construction, duplication, destruction, use-after-destruction, entitlement bypass, or resource/accounting corruption.
    * Runtime execution must not expose node secrets, private user data, randomness internal state, filesystem/process access, or other sandboxed host capabilities to attacker-controlled code.
    * An unstaked Access or Observer node must not be able to alter existing data in execution-node or consensus-node databases.
    * Standards-compliant Cadence FT/NFT or ERC20/ERC721 bridge flows must not lose, steal, permanently lock, duplicate, or mis-account escrowed assets because of Flow EVM or cross-VM handling logic.

    Rules:

    * Treat `File Name:` as the exact file/module.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Attacker may be an unprivileged transaction sender, Cadence/EVM script or contract author, Access API caller, or malicious unstaked Access/Observer node.
    * Do not rely on control of staked Collection, Consensus, Execution, or Verification nodes; admin compromise; malicious maintainer/operator; leaked private keys; compromised quorum/supermajority; unsupported local configuration; social engineering; public-mainnet testing; front-running-only attacks; or brute-force DDoS.
    * Exclude denial of service, network outages, performance degradation, griefing without unauthorized access to accounts or on-chain assets, dependency-only issues, static-analysis-only findings, gas optimizations, code style, and best-practice findings.
    * Generate 10 to 20 high-signal questions.
    * At least 70% must be multi-step flow, invariant, resource-semantics, authorization, accounting, cross-VM, state-transition, database-mutation, or cross-module questions.
    * Every question must be testable by a runnable emulator/localnet PoC, transaction/script sequence, fuzz test, invariant test, model test, or differential test.
    * Avoid generic checklist questions and repeated root causes.
    * Each question must target a plausible issue class for the exact file and scope.
    

    High-value attack surfaces:

    * Execution authorization: transaction verification, account keys, sequence numbers, payer/proposer checks, capabilities, entitlements, scheduled transactions, and account mutation.
    * Resource semantics: Cadence/FVM/EVM resource construction, moves, destruction, storage, type confusion, contract updates, system contracts, and runtime isolation.
    * Runtime secrets and sandboxing: randomness state, node private data, host filesystem/process access, memory isolation, and execution-environment privilege boundaries.
    * Unstaked Access/Observer protocol paths: request handling, ingestion, state sync, execution data requests, database writes, and any path that can alter execution or consensus node persisted data.
    * Flow EVM and cross-VM asset handling: standards-compliant FT/NFT/ERC20/ERC721 bridging, value conversion, escrow accounting, pause/association/admin/COA checks, entitlement checks, and metadata handling.

    Impact mapping:

    * Critical only: unauthorized account manipulation; resource construction, duplication, destruction, or use-after-destruction; unauthorized access to private data, node secrets, randomness state, or sandboxed host capabilities; malicious unstaked Access/Observer alteration of execution or consensus node database data; or Flow EVM/cross-VM asset theft, loss, permanent lock, escrow mis-accounting, entitlement bypass, or resource duplication.

    Each question must include:

    1. target function/module;
    2. attacker action;
    3. preconditions;
    4. call sequence;
    5. invariant tested;
    6. scoped impact;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Function: symbol_or_module] Can an attacker ACTION under PRECONDITIONS trigger CALL_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: fuzz/state-test PARAMETERS and assert EXPECTED_PROPERTY.",
    ]
    """
    return prompt


def audit_format(question: str) -> str:
    """
    Generate a focused Flow exploit-question validation prompt.
    """
    return f"""# QUESTION SCAN PROMPT

## Exploit Question
{question}

## Scope Rules
- Audit only production Flow protocol and node code listed in `scope_files`.
- Do not ask for repo contents or claim files are missing.
- Ignore tests, docs, mocks, generated files, repo automation scripts, configs, build files, IDE files, package metadata, and local deployment choices.

## Objective
Decide whether the question leads to a real, reachable Flow vulnerability.
The attacker must enter through a supported production path: transaction submission, Cadence/EVM script or contract execution, Access API input, unstaked Access node behavior, unstaked Observer node behavior, state sync, execution data request, or a database-write path reachable from those node types.
The impact must match the provided target scope.
Prefer #NoVulnerability unless the path is concrete, locally testable on unmodified Flow emulator/localnet or FLITE, and proves one of the Critical impacts in `target_scopes`.

## Method
1. Trace the attacker-controlled entrypoint.
2. Map it to exact production Flow files/functions.
3. Check relevant Flow guards: transaction authorization, capability and entitlement checks, account key and sequence validation, resource semantics, contract update checks, runtime sandboxing, randomness access, bridge accounting, Access/Observer role limits, persistence writes, or API parsing.
4. Decide whether the questioned invariant can actually break under intended deployment.
5. Prove root cause with file/function/line references.
6. Confirm realistic likelihood and exact scoped impact.
7. Reject if current validation already prevents the exploit.

## Reject Immediately
- Requires admin/operator control, leaked private keys, malicious maintainer, control of staked Collection/Consensus/Execution/Verification nodes, compromised quorum/supermajority, unsupported local configuration, social engineering, public-mainnet testing, front-running only, or brute-force DDoS.
- Only affects tests, docs, configs, scripts, mocks, generated code, local tooling, or deployment choices.
- External dependency behavior is the only cause.
- Impact is denial of service, network outage, performance degradation, griefing without unauthorized access to accounts or on-chain assets, logging, observability, local misconfiguration, harmless rejection, ordinary peer disconnect, stale read with no security impact, or theoretical risk.
- No concrete scoped impact or no realistic exploit path.

## Output
If valid:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If invalid, output exactly:
#NoVulnerability found for this question.
"""


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for Flow.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Access Rules (Strict)
- Treat production Flow files in the provided scope as accessible context.
- Do not claim missing/inaccessible files.
- Do not ask for repository contents.
- Do not scan tests, docs, build files, IDE files, configs, generated files, resources, package metadata, repo automation scripts, local tooling, or deployment-only choices as audited targets.

## Objective
Use the external report's vulnerability class as a hint to find valid issues based on Flow protocol security impact.
Focus on externally reachable Flow issues triggered by an unprivileged transaction sender, Cadence/EVM script or contract author, Access API caller, or malicious unstaked Access/Observer node.
Only report an analog if Flow code has its own reachable root cause and the impact matches the provided target scope.

## Method
1. Classify vuln type: transaction authorization, unauthorized account mutation, resource construction/duplication/destruction/use-after-destruction, entitlement/capability bypass, runtime sandbox escape, private data exposure, randomness-state exposure, unstaked Access/Observer database mutation, bridge escrow mis-accounting, cross-VM asset loss, or API validation leading to one of those impacts.
2. Map to Flow components and exact production files.
3. Prove root cause with exact file/function/module/line references.
4. Confirm concrete Flow scoped impact and realistic likelihood.
5. Explain the attacker-controlled entry path and why Flow code is a necessary vulnerable step.
6. Reject if the impact does not match the provided target scope.

## Disqualify Immediately
- No reachable attacker-controlled entry path.
- Requires admin/operator control, leaked private keys, malicious maintainer, control of staked Collection/Consensus/Execution/Verification nodes, compromised quorum/supermajority, unsupported local configuration, social engineering, public-mainnet testing, front-running only, or brute-force DDoS.
- External dependency behavior is the only cause.
- Test/docs/config/build/generated/local-tooling issue.
- Theoretical-only issue with no protocol impact.
- Impact is denial of service, network outage, performance degradation, griefing without unauthorized access to accounts or on-chain assets, local misconfiguration, observability noise, logging noise, harmless rejection, ordinary peer disconnect, stale read with no security impact, or non-security correctness.
- Impact or likelihood missing.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict Flow protocol validation prompt for security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and the linked Flow responsible-disclosure policy for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Reject admin-only, operator-only, trusted-maintainer, leaked-key, best-practice, docs/style, gas-only, denial-of-service, performance-only, griefing-only, front-running-only, static-analysis-only, dependency-only, and purely theoretical issues.
- Reject if the exploit requires unrealistic assumptions, victim mistakes, missing external context, unsupported protocol behavior, control of staked Collection/Consensus/Execution/Verification nodes, compromised quorum/supermajority, or unsupported local configuration.
- A valid report must be triggerable by an unprivileged external user through transactions/scripts/contracts/API inputs or by an unstaked Access/Observer node.
- The final impact must match one of the Critical `target_scopes`, not just a generic code bug.
- Prefer #NoVulnerability over speculative reports.

## Allowed Impact Scope
Only these impacts are valid:
- Critical. Execution-layer vulnerability causing unauthorized account manipulation or unauthorized mutation of another user account state.
- Critical. Execution-layer vulnerability circumventing Cadence/FVM/EVM resource semantics, including unauthorized resource construction, duplication, destruction, or use-after-destruction.
- Critical. Runtime or execution-environment vulnerability enabling unauthorized access to private data, node secrets, randomness state, or sandboxed host capabilities through attacker-controlled transactions or scripts.
- Critical. Protocol-layer vulnerability originating from an unstaked Access or Observer node that alters existing data in execution-node or consensus-node databases.
- Critical. Flow EVM or cross-VM asset handling vulnerability causing theft, loss, permanent lock, escrow mis-accounting, entitlement bypass, or resource duplication for standards-compliant bridged assets.

If the submitted claim does not concretely prove one of the allowed impacts above, it is invalid.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function, and line/code references.
2. Clear root cause and broken protocol/security/accounting assumption.
3. Reachable exploit path: preconditions -> attacker action -> trigger -> bad result.
4. Existing checks/guards reviewed and shown insufficient.
5. Concrete impact that exactly matches one allowed Flow impact above, with realistic likelihood.
6. Reproducible proof path: Flow CLI emulator/localnet/FLITE commands, transaction/script/contract sources, full command output, or a justified test/fuzz/invariant reproducer when emulator/localnet cannot demonstrate the impact.
7. No obvious rejection reason from SECURITY.md, known issues, privileges, or scope exclusions.

## Silent Triage Questions
Before output, internally answer:
- Can a normal external user or unstaked Access/Observer node trigger this?
- Does the code actually behave as claimed?
- Is the impact caused by this protocol, not by an external dependency alone?
- Is the account/resource/asset/database impact concrete, not hypothetical?
- Would a responsible-disclosure triager accept the proof?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete allowed Flow impact and severity rationale]

## Likelihood Explanation
[Attacker capability, required conditions, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or fuzz/invariant/model/restart test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt

