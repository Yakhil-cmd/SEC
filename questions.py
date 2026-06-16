import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the path from https:///github.com/dfinity/ICRC-1
SOURCE_REPO = "codertjay/zksync-os"
# todo: the name of the repository
REPO_NAME = "zksync-os"
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
    "basic_bootloader/src/bootloader/block_flow/block_data_keeper.rs",
    "basic_bootloader/src/bootloader/block_flow/chain_check.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/block_data.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/block_hashes_cache.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/block_header.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/eip_2935_historical_block_hash/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/eip_4788_historical_beacon_root/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/eip_6110_deposit_events_parser/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/eip_7002_withdrawal_contract/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/eip_7251_consolidation_contract/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_152/impls.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_152/mixing_function.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_152/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/addition.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/addresses.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mappings.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/pairing.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/hooks/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/loop_op.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/metadata_op.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/oracle_queries/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/post_init_op.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_proving.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/post_tx_op_sequencing.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/pre_tx_loop.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/rlp_encodings/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/rlp_encodings/receipt.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/rlp_encodings/utils.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/utils.rs",
    "basic_bootloader/src/bootloader/block_flow/ethereum/withdrawals.rs",
    "basic_bootloader/src/bootloader/block_flow/metadata_init_op.rs",
    "basic_bootloader/src/bootloader/block_flow/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/post_system_init_op.rs",
    "basic_bootloader/src/bootloader/block_flow/post_tx_loop_op.rs",
    "basic_bootloader/src/bootloader/block_flow/pre_tx_loop_op.rs",
    "basic_bootloader/src/bootloader/block_flow/tx_loop.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/block_data.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/metadata_op.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_init_op.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/da_commitment_generator/blake2s_commitment_generator.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/da_commitment_generator/blob_commitment_generator/brp_roots_of_unity.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/da_commitment_generator/blob_commitment_generator/commitment_and_proof_advice.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/da_commitment_generator/blob_commitment_generator/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/da_commitment_generator/blob_commitment_generator/polynomial_evaluation.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/da_commitment_generator/keccak256_commitment_generator.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/da_commitment_generator/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_multiblock_batch.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_sequencing.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/pre_tx_loop.rs",
    "basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs",
    "basic_bootloader/src/bootloader/block_header.rs",
    "basic_bootloader/src/bootloader/config.rs",
    "basic_bootloader/src/bootloader/constants.rs",
    "basic_bootloader/src/bootloader/errors.rs",
    "basic_bootloader/src/bootloader/mod.rs",
    "basic_bootloader/src/bootloader/result_keeper.rs",
    "basic_bootloader/src/bootloader/rlp.rs",
    "basic_bootloader/src/bootloader/run_single_interaction.rs",
    "basic_bootloader/src/bootloader/runner.rs",
    "basic_bootloader/src/bootloader/stf.rs",
    "basic_bootloader/src/bootloader/supported_ees.rs",
    "basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs",
    "basic_bootloader/src/bootloader/transaction/abi_encoded/u256be_ptr.rs",
    "basic_bootloader/src/bootloader/transaction/access_list.rs",
    "basic_bootloader/src/bootloader/transaction/authorization_list.rs",
    "basic_bootloader/src/bootloader/transaction/blobs.rs",
    "basic_bootloader/src/bootloader/transaction/mod.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/eip_2718_tx_envelope.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/mod.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/rlp/minimal_rlp_parser.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/rlp/mod.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/eip_1559_tx.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/eip_2930_tx.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/eip_4844_tx.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/eip_7702_tx.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/mod.rs",
    "basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/service_tx.rs",
    "basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs",
    "basic_bootloader/src/bootloader/transaction_flow/ethereum/tx_level_metadata.rs",
    "basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs",
    "basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs",
    "basic_bootloader/src/bootloader/transaction_flow/mod.rs",
    "basic_bootloader/src/bootloader/transaction_flow/process_transaction.rs",
    "basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs",
    "basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs",
    "basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs",
    "basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs",
    "basic_bootloader/src/lib.rs",
    "basic_system/src/cost_constants.rs",
    "basic_system/src/lib.rs",
    "basic_system/src/system_functions/bn254_ecadd.rs",
    "basic_system/src/system_functions/bn254_ecmul.rs",
    "basic_system/src/system_functions/bn254_pairing_check.rs",
    "basic_system/src/system_functions/ecrecover.rs",
    "basic_system/src/system_functions/keccak256.rs",
    "basic_system/src/system_functions/mod.rs",
    "basic_system/src/system_functions/modexp/delegation/bigint.rs",
    "basic_system/src/system_functions/modexp/delegation/mod.rs",
    "basic_system/src/system_functions/modexp/delegation/u256.rs",
    "basic_system/src/system_functions/modexp/mod.rs",
    "basic_system/src/system_functions/p256_verify.rs",
    "basic_system/src/system_functions/point_evaluation.rs",
    "basic_system/src/system_functions/ripemd160.rs",
    "basic_system/src/system_functions/sha256.rs",
    "basic_system/src/system_implementation/caches/basic_account_properties.rs",
    "basic_system/src/system_implementation/caches/cache_element_properties.rs",
    "basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs",
    "basic_system/src/system_implementation/caches/mod.rs",
    "basic_system/src/system_implementation/caches/storage_access_policy.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/caches/account_cache.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/caches/account_properties.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/caches/full_storage_cache.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/caches/mod.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/caches/preimage.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/cost_constants.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mod.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/interner/ext_impls.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/interner/mod.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/lazy_leaf_value.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/mod.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/nodes.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/parse_node.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/preimages.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/rlp.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/trie.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/delete_from_branch.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/delete_leaf.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/delete_subtree.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/insert_new_leaf_into_branch.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/mod.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/reattach.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/split_existing.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/split_extension.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/split_leaf.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/mpt/updates/update_leaf_value.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/persist_changes.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/storage_model.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/alias.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/alloc/btreemap.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/alloc/btreeset.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/alloc/deque.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/alloc/mod.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/alloc/vec/global_alloc.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/alloc/vec/mod.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/alloc/vec/with_allocator.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/mod.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/std/hashmap.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/std/hashset.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/impls/std/mod.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/lib.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/macros.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/supporting_crates/cc-traits/src/non_alias.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/vec_trait/bi_vec.rs",
    "basic_system/src/system_implementation/ethereum_storage_model/vec_trait/mod.rs",
    "basic_system/src/system_implementation/flat_storage_model/account_cache.rs",
    "basic_system/src/system_implementation/flat_storage_model/account_cache_entry.rs",
    "basic_system/src/system_implementation/flat_storage_model/cost_constants.rs",
    "basic_system/src/system_implementation/flat_storage_model/mod.rs",
    "basic_system/src/system_implementation/flat_storage_model/preimage_cache.rs",
    "basic_system/src/system_implementation/flat_storage_model/simple_growable_storage.rs",
    "basic_system/src/system_implementation/flat_storage_model/storage_cache.rs",
    "basic_system/src/system_implementation/mod.rs",
    "basic_system/src/system_implementation/system/interop_roots.rs",
    "basic_system/src/system_implementation/system/io_subsystem.rs",
    "basic_system/src/system_implementation/system/mod.rs",
    "callable_oracles/src/arithmetic/mod.rs",
    "callable_oracles/src/blob_kzg_commitment/mod.rs",
    "callable_oracles/src/hash_to_prime/common.rs",
    "callable_oracles/src/hash_to_prime/compute.rs",
    "callable_oracles/src/hash_to_prime/evaluate.rs",
    "callable_oracles/src/hash_to_prime/mod.rs",
    "callable_oracles/src/hash_to_prime/verify.rs",
    "callable_oracles/src/lib.rs",
    "callable_oracles/src/utils/evaluate.rs",
    "callable_oracles/src/utils/mod.rs",
    "callable_oracles/src/utils/usize_slice_iterator.rs",
    "crypto/src/ark_ff_delegation/biginteger/arithmetic.rs",
    "crypto/src/ark_ff_delegation/biginteger/mod.rs",
    "crypto/src/ark_ff_delegation/const_helpers.rs",
    "crypto/src/ark_ff_delegation/fp/mod.rs",
    "crypto/src/ark_ff_delegation/fp/montgomery_backend.rs",
    "crypto/src/ark_ff_delegation/mod.rs",
    "crypto/src/bigint_delegation/delegation.rs",
    "crypto/src/bigint_delegation/mod.rs",
    "crypto/src/bigint_delegation/u256.rs",
    "crypto/src/bigint_delegation/u512.rs",
    "crypto/src/blake2s/delegated_extended.rs",
    "crypto/src/blake2s/mod.rs",
    "crypto/src/blake2s/naive.rs",
    "crypto/src/bls12_381/consts.rs",
    "crypto/src/bls12_381/curves/g1.rs",
    "crypto/src/bls12_381/curves/g1_swu_iso.rs",
    "crypto/src/bls12_381/curves/g2.rs",
    "crypto/src/bls12_381/curves/g2_swu_iso.rs",
    "crypto/src/bls12_381/curves/mod.rs",
    "crypto/src/bls12_381/curves/pairing_impl.rs",
    "crypto/src/bls12_381/curves/util.rs",
    "crypto/src/bls12_381/eip2537.rs",
    "crypto/src/bls12_381/fields/fq.rs",
    "crypto/src/bls12_381/fields/fq12.rs",
    "crypto/src/bls12_381/fields/fq2.rs",
    "crypto/src/bls12_381/fields/fq6.rs",
    "crypto/src/bls12_381/fields/fr.rs",
    "crypto/src/bls12_381/fields/mod.rs",
    "crypto/src/bls12_381/mod.rs",
    "crypto/src/bn254/curves/g1.rs",
    "crypto/src/bn254/curves/g2.rs",
    "crypto/src/bn254/curves/mod.rs",
    "crypto/src/bn254/curves/pairing_impl.rs",
    "crypto/src/bn254/fields/fq.rs",
    "crypto/src/bn254/fields/fq12.rs",
    "crypto/src/bn254/fields/fq2.rs",
    "crypto/src/bn254/fields/fq6.rs",
    "crypto/src/bn254/fields/fr.rs",
    "crypto/src/bn254/fields/mod.rs",
    "crypto/src/bn254/mod.rs",
    "crypto/src/glv_decomposition.rs",
    "crypto/src/k256/mod.rs",
    "crypto/src/lib.rs",
    "crypto/src/p256/mod.rs",
    "crypto/src/raw_delegation_interface.rs",
    "crypto/src/ripemd160/mod.rs",
    "crypto/src/secp256k1/context.rs",
    "crypto/src/secp256k1/field/field_10x26.rs",
    "crypto/src/secp256k1/field/field_5x52.rs",
    "crypto/src/secp256k1/field/field_8x32.rs",
    "crypto/src/secp256k1/field/field_impl.rs",
    "crypto/src/secp256k1/field/mod.rs",
    "crypto/src/secp256k1/field/mod_inv32.rs",
    "crypto/src/secp256k1/field/mod_inv64.rs",
    "crypto/src/secp256k1/mod.rs",
    "crypto/src/secp256k1/points/affine.rs",
    "crypto/src/secp256k1/points/jacobian.rs",
    "crypto/src/secp256k1/points/mod.rs",
    "crypto/src/secp256k1/points/storage.rs",
    "crypto/src/secp256k1/recover.rs",
    "crypto/src/secp256k1/scalars/invert.rs",
    "crypto/src/secp256k1/scalars/mod.rs",
    "crypto/src/secp256k1/scalars/scalar32.rs",
    "crypto/src/secp256k1/scalars/scalar32_delegation.rs",
    "crypto/src/secp256k1/scalars/scalar64.rs",
    "crypto/src/secp256r1/context.rs",
    "crypto/src/secp256r1/field/fe32_delegation.rs",
    "crypto/src/secp256r1/field/fe64.rs",
    "crypto/src/secp256r1/field/mod.rs",
    "crypto/src/secp256r1/mod.rs",
    "crypto/src/secp256r1/points/affine.rs",
    "crypto/src/secp256r1/points/jacobian.rs",
    "crypto/src/secp256r1/points/mod.rs",
    "crypto/src/secp256r1/points/storage.rs",
    "crypto/src/secp256r1/scalar/mod.rs",
    "crypto/src/secp256r1/scalar/scalar64.rs",
    "crypto/src/secp256r1/scalar/scalar_delegation.rs",
    "crypto/src/secp256r1/u64_arithmetic.rs",
    "crypto/src/secp256r1/verify.rs",
    "crypto/src/secp256r1/wnaf.rs",
    "crypto/src/sha256/mod.rs",
    "crypto/src/sha3/mod.rs",
    "evm_interpreter/src/ee_trait_impl.rs",
    "evm_interpreter/src/errors.rs",
    "evm_interpreter/src/evm_stack.rs",
    "evm_interpreter/src/gas.rs",
    "evm_interpreter/src/gas_constants.rs",
    "evm_interpreter/src/i256.rs",
    "evm_interpreter/src/instructions/arithmetic.rs",
    "evm_interpreter/src/instructions/bitwise.rs",
    "evm_interpreter/src/instructions/control_flow.rs",
    "evm_interpreter/src/instructions/environment.rs",
    "evm_interpreter/src/instructions/heap.rs",
    "evm_interpreter/src/instructions/host.rs",
    "evm_interpreter/src/instructions/mod.rs",
    "evm_interpreter/src/instructions/stack.rs",
    "evm_interpreter/src/instructions/system.rs",
    "evm_interpreter/src/interpreter.rs",
    "evm_interpreter/src/lib.rs",
    "evm_interpreter/src/native_resource_constants.rs",
    "evm_interpreter/src/opcodes.rs",
    "evm_interpreter/src/precompile_addresses.rs",
    "evm_interpreter/src/u256.rs",
    "evm_interpreter/src/utils.rs",
    "oracle_provider/src/lib.rs",
    "proof_running_system/src/io_oracle/mod.rs",
    "proof_running_system/src/lib.rs",
    "proof_running_system/src/system/bootloader.rs",
    "proof_running_system/src/system/mod.rs",
    "proof_running_system/src/talc/mod.rs",
    "storage_models/src/common_structs/generic_transient_storage.rs",
    "storage_models/src/common_structs/mod.rs",
    "storage_models/src/common_structs/traits/mod.rs",
    "storage_models/src/common_structs/traits/preimage_cache_model.rs",
    "storage_models/src/common_structs/traits/snapshottable_io.rs",
    "storage_models/src/common_structs/traits/storage_cache_model.rs",
    "storage_models/src/common_structs/traits/storage_model.rs",
    "storage_models/src/lib.rs",
    "supporting_crates/delegated_u256/src/arithmetic.rs",
    "supporting_crates/delegated_u256/src/copy.rs",
    "supporting_crates/delegated_u256/src/delegation.rs",
    "supporting_crates/delegated_u256/src/lib.rs",
    "supporting_crates/delegated_u256/src/utils.rs",
    "supporting_crates/modexp/src/arith.rs",
    "supporting_crates/modexp/src/lib.rs",
    "supporting_crates/modexp/src/mpnat.rs",
    "supporting_crates/u256/src/lib.rs",
    "supporting_crates/u256/src/naive/mod.rs",
    "supporting_crates/u256/src/risc_v/mod.rs",
    "system_hooks/src/addresses_constants.rs",
    "system_hooks/src/call_hooks/contract_deployer_temp.rs",
    "system_hooks/src/call_hooks/l1_messenger.rs",
    "system_hooks/src/call_hooks/mint_base_token.rs",
    "system_hooks/src/call_hooks/mod.rs",
    "system_hooks/src/call_hooks/precompiles.rs",
    "system_hooks/src/call_hooks/set_bytecode_on_address.rs",
    "system_hooks/src/event_hooks/interop_root_reporter.rs",
    "system_hooks/src/event_hooks/mod.rs",
    "system_hooks/src/event_hooks/system_context.rs",
    "system_hooks/src/lib.rs",
    "zk_ee/src/common_structs/cache_record.rs",
    "zk_ee/src/common_structs/callee_account_properties.rs",
    "zk_ee/src/common_structs/da_commitment_scheme.rs",
    "zk_ee/src/common_structs/events_storage.rs",
    "zk_ee/src/common_structs/history_counter.rs",
    "zk_ee/src/common_structs/history_list.rs",
    "zk_ee/src/common_structs/history_map/element_pool.rs",
    "zk_ee/src/common_structs/history_map/element_with_history.rs",
    "zk_ee/src/common_structs/history_map/mod.rs",
    "zk_ee/src/common_structs/interop_root_storage.rs",
    "zk_ee/src/common_structs/logs_storage.rs",
    "zk_ee/src/common_structs/mod.rs",
    "zk_ee/src/common_structs/new_preimages_publication_storage.rs",
    "zk_ee/src/common_structs/new_settlement_layer_chain_id_storage.rs",
    "zk_ee/src/common_structs/proof_data.rs",
    "zk_ee/src/common_structs/pubdata_compression.rs",
    "zk_ee/src/common_structs/skip_list_quasi_vec/mod.rs",
    "zk_ee/src/common_structs/state_root_view.rs",
    "zk_ee/src/common_structs/system_hooks.rs",
    "zk_ee/src/common_structs/warm_storage_key.rs",
    "zk_ee/src/common_structs/warm_storage_value.rs",
    "zk_ee/src/common_traits/key_like_with_bounds.rs",
    "zk_ee/src/common_traits/mod.rs",
    "zk_ee/src/execution_environment_type.rs",
    "zk_ee/src/lib.rs",
    "zk_ee/src/memory/allocator_ext.rs",
    "zk_ee/src/memory/byte_slice.rs",
    "zk_ee/src/memory/mod.rs",
    "zk_ee/src/memory/slice_vec.rs",
    "zk_ee/src/memory/stack_implementations/mod.rs",
    "zk_ee/src/memory/stack_implementations/skip_list_stack.rs",
    "zk_ee/src/memory/stack_implementations/vec_stack.rs",
    "zk_ee/src/memory/stack_trait.rs",
    "zk_ee/src/oracle/basic_queries.rs",
    "zk_ee/src/oracle/mod.rs",
    "zk_ee/src/oracle/query_ids.rs",
    "zk_ee/src/oracle/simple_oracle_query.rs",
    "zk_ee/src/oracle/usize_serialization/dyn_usize_iterator.rs",
    "zk_ee/src/oracle/usize_serialization/mod.rs",
    "zk_ee/src/reference_implementations/mod.rs",
    "zk_ee/src/storage_types/mod.rs",
    "zk_ee/src/system/base_system_functions.rs",
    "zk_ee/src/system/call_modifiers.rs",
    "zk_ee/src/system/constants.rs",
    "zk_ee/src/system/errors/cascade.rs",
    "zk_ee/src/system/errors/context/contextualized.rs",
    "zk_ee/src/system/errors/context/element.rs",
    "zk_ee/src/system/errors/context/empty.rs",
    "zk_ee/src/system/errors/context/mod.rs",
    "zk_ee/src/system/errors/context/nonempty.rs",
    "zk_ee/src/system/errors/display.rs",
    "zk_ee/src/system/errors/interface.rs",
    "zk_ee/src/system/errors/internal.rs",
    "zk_ee/src/system/errors/location.rs",
    "zk_ee/src/system/errors/metadata.rs",
    "zk_ee/src/system/errors/mod.rs",
    "zk_ee/src/system/errors/no_errors.rs",
    "zk_ee/src/system/errors/root_cause.rs",
    "zk_ee/src/system/errors/runtime.rs",
    "zk_ee/src/system/errors/subsystem.rs",
    "zk_ee/src/system/errors/system.rs",
    "zk_ee/src/system/execution_environment/call_params.rs",
    "zk_ee/src/system/execution_environment/environment_state.rs",
    "zk_ee/src/system/execution_environment/evm/errors.rs",
    "zk_ee/src/system/execution_environment/evm/mod.rs",
    "zk_ee/src/system/execution_environment/mod.rs",
    "zk_ee/src/system/io.rs",
    "zk_ee/src/system/logger.rs",
    "zk_ee/src/system/metadata/basic_metadata.rs",
    "zk_ee/src/system/metadata/dynamic_metadata_responder.rs",
    "zk_ee/src/system/metadata/mod.rs",
    "zk_ee/src/system/metadata/system_metadata.rs",
    "zk_ee/src/system/metadata/zk_metadata.rs",
    "zk_ee/src/system/mod.rs",
    "zk_ee/src/system/resources.rs",
    "zk_ee/src/system/result_keeper.rs",
    "zk_ee/src/system/tracer/evm_tracer.rs",
    "zk_ee/src/system/tracer/mod.rs",
    "zk_ee/src/system/validator/mod.rs",
    "zk_ee/src/types_config/mod.rs",
    "zk_ee/src/utils/aligned_vector.rs",
    "zk_ee/src/utils/bytes32.rs",
    "zk_ee/src/utils/cheap_clone.rs",
    "zk_ee/src/utils/convenience/memcopy.rs",
    "zk_ee/src/utils/convenience/mod.rs",
    "zk_ee/src/utils/exact_size_chain.rs",
    "zk_ee/src/utils/integer_utils.rs",
    "zk_ee/src/utils/mod.rs",
    "zk_ee/src/utils/stack_linked_list.rs",
    "zk_ee/src/utils/type_assert.rs",
    "zk_ee/src/utils/usize_rw.rs",
    "zk_ee/src/utils/write_bytes.rs",
    "zksync_os/src/asm/asm_reduced.S",
    "zksync_os/src/helper_reg_utils.rs",
    "zksync_os/src/main.rs",
    "zksync_os/src/memcpy.s",
    "zksync_os/src/memset.s",
    "zksync_os/src/quasi_uart.rs",
    "zksync_os/src/trap_frame.rs",
    "zksync_os/src/utils.rs",
]

target_scopes = [
    "Critical. Direct and publicly triggerable loss of funds",
    "High. Underconstraints in the circuit that make invalid ZKsync OS executions provable",
    "High. Circuit, node, or program mismatches that make valid ZKsync OS executions unprovable and require verification key regeneration",
    "Medium. Undocumented deviation from EVM behavior",
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit + fuzzing questions for one ZKsync OS target.

    ```
    target_file format:
    "'File Name: basic_bootloader/src/lib.rs -> Scope: Critical Direct and publicly triggerable loss of funds'"
    """

    prompt = f"""
    ```
    
    Generate exploit-focused security audit and fuzzing questions for this exact ZKsync OS target:
    
    {target_file}
    
    Use live context from the project if available: bootloader flow, system interfaces, oracle IO, storage model, EVM interpreter, hooks/precompiles, crypto helpers, resource accounting, and RISC-V proving target.
    
    Protocol focus:
    ZKsync OS is a security-critical state transition function for a ZK rollup, executed in forward host mode and proving RISC-V mode, with Airbender proofs expected to validate the same execution semantics.
    
    Core invariants:
    
    * Invalid state transitions, oracle answers, storage writes, precompile results, or transaction effects must never be accepted or provable.
    * Forward and proving execution must agree on gas/resources, memory, hashing, crypto, storage, logs, errors, and observable EVM semantics.
    * Circuit constraints must prevent invalid ZKsync OS executions from becoming valid proofs.
    * Valid ZKsync OS executions must remain provable without circuit/node/program mismatch or verification key regeneration.
    * Public transaction, call, precompile, and bootloader paths must not cause direct loss of funds.
    * EVM behavior deviations must be documented, intentional, and consistently enforced across modes.
    
    Rules:
    
    * Treat `File Name:` as the exact file/module.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Attacker is unprivileged: a transaction sender, contract caller, eth_call/estimate_gas simulator, block input provider, oracle-data-influencing caller, or public precompile/system-hook caller.
    * Do not rely on admin compromise, malicious governance, leaked keys, third-party oracle lies, Sybil/51% attacks, phishing, or public-mainnet testing.
    * Generate 10 to 20 high-signal questions.
    * At least 70% must be multi-step flow, invariant, fuzz, accounting, state-transition, or cross-module questions.
    * Every question must be testable by PoC, unit test, fuzz test, invariant test, or differential test.
    * Avoid generic checklist questions and repeated root causes.
    * Note any question u must target valid issue u think could be possible 
    
    High-value attack surfaces:
    
    * Bootloader transaction flow: validation skipping for simulation, fee/resource accounting, refunds, batch/block state, and error boundaries.
    * Forward/proving divergence: RISC-V no-std code, allocator assumptions, oracle queries, memory layout, serialization, hashing, and result encoding.
    * EVM interpreter semantics: opcode edge cases, gas rules, call/create/revert behavior, logs, return data, and undocumented compatibility gaps.
    * System hooks and precompiles: dispatch, input parsing, crypto/modexp/u256 arithmetic, memory bounds, and return status handling.
    * Storage and IO: slot reads/writes, transient state, access lists, pubdata/DA commitments, non-determinism routing, and rollback behavior.
    * Circuit/protocol alignment: underconstrained outputs, invalid-but-provable execution, valid-but-unprovable execution, and VK-changing mismatches.
    
    Impact mapping:
    
    * Loss of funds: Public attacker directly triggers theft, burn, unauthorized transfer, fee/refund drain, or unrecoverable balance loss.
    * Invalid provable execution: Missing constraints allow an invalid ZKsync OS execution to produce an accepted proof.
    * Valid unprovable execution: Circuit, node, or program mismatch makes valid execution unprovable and requires verification key regeneration.
    * EVM deviation: Behavior differs from EVM without documentation and is reachable through production execution.
    
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
    "[File: {target_file}] [Function: symbol_or_module] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger CALL_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: fuzz/state-test PARAMETERS and assert EXPECTED_PROPERTY.",
    ]
    """
    return prompt


def audit_format(question: str) -> str:
    """
    Generate a focused ZKsync OS exploit-question validation prompt.
    """
    return f"""# QUESTION SCAN PROMPT

## Exploit Question
{question}

## Scope Rules
- Audit only production ZKsync OS code in the provided Immunefi scope.
- Do not ask for repo contents or claim files are missing.
- Ignore tests, docs, mocks, generated files, scripts, configs, build files, IDE files, and package metadata.

## Objective
Decide whether the question leads to a real, reachable ZKsync OS vulnerability.
The attacker must be unprivileged and enter through transaction execution, call simulation, public contract execution, system hooks, precompiles, oracle IO, bootloader processing, or proving/forward execution inputs.
The impact must match the provided target scope.
Prefer #NoVulnerability unless the path is concrete, local-testable, and bounty-grade.

## Method
1. Trace the attacker-controlled entrypoint.
2. Map it to exact production ZKsync OS files/functions.
3. Check the relevant guard: tx validation, simulation skip logic, gas/resource accounting, storage rollback, oracle query routing, memory bounds, crypto/precompile parsing, or forward/proving consistency.
4. Decide whether the questioned invariant can actually break under intended deployment.
5. Prove root cause with file/function/line references.
6. Confirm realistic likelihood and exact scoped impact.
7. Reject if current validation already prevents the exploit.

## Reject Immediately
- Requires trusted role, leaked key, malicious governance majority, or privileged operator access.
- Requires third-party oracle lying, Sybil/51% attack, phishing, public-mainnet testing, or DDoS/brute force.
- Only affects tests, docs, configs, scripts, mocks, generated code, or local deployment choices.
- External dependency behavior is the only cause.
- Impact is only logging, observability, local misconfiguration, non-security correctness, harmless revert, stale read, rejected transaction, or theoretical risk.
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
    Generate a short cross-project analog scan prompt for ZKsync OS.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Access Rules (Strict)
- Treat production ZKsync OS files in the provided scope as accessible context.
- Do not claim missing/inaccessible files.
- Do not ask for repository contents.
- Do not scan tests, docs, build files, IDE files, configs, generated files, resources, or package metadata as audited targets.

## Objective
Use the external report's vulnerability class as a hint to find valid issues based on ZKsync OS Immunefi scope.
Focus on externally reachable ZKsync OS issues triggered by an unprivileged transaction sender, contract caller, simulator, oracle-data-influencing caller, precompile/system-hook caller, or prover/forward execution input.
Only report an analog if ZKsync OS code has its own reachable root cause and the impact matches the provided target scope.

## Method
1. Classify vuln type: state-transition bug, forward/proving divergence, EVM semantic mismatch, underconstraint, valid-execution unprovability, resource accounting bug, storage rollback bug, oracle IO mismatch, crypto/precompile parsing bug, memory bounds issue, or public funds-loss path.
2. Map to ZKsync OS components and exact production files.
3. Prove root cause with exact file/function/module/line references.
4. Confirm concrete ZKsync OS scoped impact and realistic likelihood.
5. Explain the attacker-controlled entry path and why ZKsync OS code is a necessary vulnerable step.
6. Reject if the impact does not match the provided target scope.

## Disqualify Immediately
- No reachable attacker-controlled entry path.
- Requires trusted role, leaked key, malicious governance majority, or privileged operator access.
- Requires third-party oracle lying, Sybil/51% attack, phishing, public-mainnet testing, or DDoS/brute force.
- External dependency behavior is the only cause.
- Test/docs/config/build-only issue.
- Theoretical-only issue with no protocol impact.
- Impact is only local misconfiguration, observability noise, logging noise, harmless revert, stale read, or non-security correctness.
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
