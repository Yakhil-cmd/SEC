### Title
Unbounded Authorization List Loop in EIP-7702 Transaction Processing — (`basic_bootloader/src/bootloader/transaction/authorization_list.rs`)

---

### Summary

`parse_authorization_list_and_apply_delegations` iterates over the EIP-7702 authorization list with no upper-bound check on the number of entries. Unlike the blob list, which is explicitly capped at `MAX_BLOBS_PER_BLOCK`, the authorization list has no analogous size limit. Each iteration performs ecrecover, Keccak256 hashing, and storage reads/writes. An attacker can craft a transaction with an arbitrarily large authorization list (bounded only by the raw transaction byte length, up to ~4 GB), causing excessive native resource consumption and potential forward/proving divergence.

---

### Finding Description

In `parse_authorization_list_and_apply_delegations`, the loop at line 38 iterates unconditionally over every entry in the authorization list: [1](#0-0) 

Each call to `validate_and_apply_delegation` performs:
- A native resource charge for `PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD` (line 100–105)
- A Keccak256 hash via `compute_auth_message_signed_hash` (line 123–131), which calls `charge_keccak`
- An ecrecover via `recover_authority` (line 132–136), which calls `secp256k1_ec_recover`
- A storage read of the authority account (line 140–151)
- Conditional storage writes: `set_delegation` and `increment_nonce` (lines 186–205) [2](#0-1) 

The EIP-7702 transaction parser only rejects an **empty** authorization list; it imposes no maximum: [3](#0-2) 

Contrast this with the blob list, which has an explicit cap enforced before the loop: [4](#0-3) 

The authorization list processing is invoked from the ZK validation path with `with_infinite_ergs`, meaning EVM gas is not consumed during the loop — only native resources are charged: [5](#0-4) 

The intrinsic native pre-charge is computed from `authorization_list_num * L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION`: [6](#0-5) 

However, the pre-charge is derived from the attacker-supplied `authorization_list_num` field in the transaction, and the loop runs exactly that many times. If the constant `L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION` underestimates the actual proving cost per entry (ecrecover + keccak + storage), the attacker pays less native than the prover consumes — a resource accounting bug causing forward/proving divergence.

---

### Impact Explanation

**Vulnerability class**: Resource accounting bug / forward–proving divergence.

1. **Sequencer DoS**: A transaction with tens of thousands of authorization entries forces the sequencer to execute ecrecover + keccak + storage I/O for each entry before the transaction can be rejected or accepted. The transaction byte-length limit (~4 GB via `u32::MAX`) allows millions of minimal entries.

2. **Forward/proving divergence**: If `L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION` does not precisely cover the proving cost of one full authorization entry (ecrecover is among the most expensive operations to prove), the prover exhausts more native resources than the sequencer charged. This breaks the invariant that forward execution and proof generation consume equivalent resources, which is a core correctness requirement of ZKsync OS.

3. **State corruption risk**: Because `set_delegation` and `increment_nonce` are called for each valid entry, a large list with many valid entries causes unbounded state mutations within a single transaction, amplifying pubdata and storage diff costs beyond what was pre-charged.

---

### Likelihood Explanation

- EIP-7702 is feature-gated (`#[cfg(feature = "eip-7702")]`), but it is an active production feature with test coverage in the repository.
- Any unprivileged user can submit an EIP-7702 transaction with an arbitrarily large authorization list — no special role or key is required.
- The attack requires only constructing a valid RLP-encoded EIP-7702 transaction with many authorization entries, which is trivially achievable with standard tooling.
- The gas limit bounds the total native budget, but if the per-entry native constant is even slightly underestimated, the divergence compounds linearly with list size.

---

### Recommendation

Add an explicit upper-bound check on the authorization list size before the loop, mirroring the blob list pattern:

```rust
// In parse_authorization_list_and_apply_delegations or at parse time:
const MAX_AUTHORIZATION_LIST_SIZE: usize = <chosen_limit>;
if auth_list.count.unwrap_or(0) > MAX_AUTHORIZATION_LIST_SIZE {
    return Err(TxError::Validation(InvalidTransaction::AuthListTooLong));
}
```

This check should be placed in the EIP-7702 transaction parser (`eip_7702_tx.rs`) analogously to the blob list check in `parse_blobs_list`, so the list size is rejected at decode time rather than at processing time. [7](#0-6) 

Additionally, audit `L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION` to confirm it fully covers the proving cost of one ecrecover + keccak + storage read/write cycle.

---

### Proof of Concept

1. Construct an EIP-7702 transaction with `N = 10_000` authorization entries, each with a valid-length but invalid signature (so `validate_and_apply_delegation` returns `false` after ecrecover, but still executes the full ecrecover + keccak path).
2. Set `gas_limit` to `MAX_BLOCK_GAS_LIMIT`.
3. Submit the transaction to the sequencer.
4. The sequencer executes the loop 10,000 times, each performing ecrecover + keccak + storage read, before the transaction completes.
5. Compare the native resources consumed in forward execution against the native resources consumed during proof generation for the same transaction. If `L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION` is underestimated, the prover will report higher native usage than the sequencer charged, demonstrating forward/proving divergence. [8](#0-7) [9](#0-8)

### Citations

**File:** basic_bootloader/src/bootloader/transaction/authorization_list.rs (L27-61)
```rust
pub fn parse_authorization_list_and_apply_delegations<S: EthereumLikeTypes>(
    system: &mut System<S>,
    resources: &mut S::Resources,
    auth_list: AuthorizationList<'_>,
) -> Result<(), TxError>
where
    S::IO: IOSubsystemExt,
{
    use crate::bootloader::transaction::rlp_encoded::AuthorizationEntry;
    let mut hasher = crypto::sha3::Keccak256::new();

    for entry in auth_list.iter() {
        let AuthorizationEntry {
            chain_id,
            address,
            nonce,
            y_parity,
            r,
            s,
        } = entry;
        let success = validate_and_apply_delegation(
            system,
            resources,
            &chain_id,
            nonce,
            address,
            (y_parity, r, s),
            &mut hasher,
        )?;
        system_log!(system, "Delegation success: {success}\n");

        if !success {}
    }
    Ok(())
}
```

**File:** basic_bootloader/src/bootloader/transaction/authorization_list.rs (L85-136)
```rust
fn validate_and_apply_delegation<S: EthereumLikeTypes>(
    system: &mut System<S>,
    resources: &mut S::Resources,
    auth_chain_id: &U256,
    auth_nonce: u64,
    delegation_address: &[u8; 20],
    auth_sig_data: (u8, &[u8], &[u8]),
    hasher: &mut crypto::sha3::Keccak256,
) -> Result<bool, TxError>
where
    S::IO: IOSubsystemExt,
{
    let chain_id = system.get_chain_id();

    // 0. Pre-charge intrinsic gas
    resources.charge(&S::Resources::from_ergs_and_native(
        Ergs(evm_interpreter::gas_constants::NEWACCOUNT * ERGS_PER_GAS),
        <<S::Resources as Resources>::Native as zk_ee::system::Computational>::from_computational(
            crate::bootloader::constants::PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD,
        ),
    ))?;

    // 1. Check chain id
    if !auth_chain_id.is_zero() && auth_chain_id != &U256::from(chain_id) {
        return Ok(false);
    }
    // 2. Check for nonce overflow
    if auth_nonce == u64::MAX {
        return Ok(false);
    }
    // 3. Signature
    // EIP-2 check
    let (_, _, auth_s) = auth_sig_data;
    let s = U256::try_from_be_slice(auth_s)
        .ok_or::<TxError>(InvalidTransaction::InvalidStructure.into())?;
    if s > crypto::secp256k1::SECP256K1N_HALF_U256 {
        return Ok(false);
    }
    let msg = resources.with_infinite_ergs(|inf_ergs| {
        compute_auth_message_signed_hash::<S>(
            inf_ergs,
            auth_chain_id,
            auth_nonce,
            delegation_address,
            hasher,
        )
    })?;
    let Some(authority) = resources
        .with_infinite_ergs(|inf_ergs| recover_authority(system, inf_ergs, auth_sig_data, &msg))?
    else {
        return Ok(false);
    };
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/eip_7702_tx.rs (L100-118)
```rust
        let access_list = AccessList::decode_list_from(r)?;
        let authorization_list = AuthorizationList::decode_list_from(r)?;

        if authorization_list.count == Some(0) {
            return Err(InvalidTransaction::AuthListIsEmpty);
        }
        Ok(Self {
            chain_id,
            nonce,
            max_priority_fee_per_gas,
            max_fee_per_gas,
            gas_limit,
            to,
            value,
            data,
            access_list,
            authorization_list,
        })
    }
```

**File:** basic_bootloader/src/bootloader/transaction/blobs.rs (L7-13)
```rust
pub fn parse_blobs_list<const MAX_BLOBS_IN_TX: usize>(
    blobs_list: BlobHashesList<'_>,
) -> Result<arrayvec::ArrayVec<Bytes32, MAX_BLOBS_IN_TX>, TxError> {
    let mut result = arrayvec::ArrayVec::<_, MAX_BLOBS_IN_TX>::new();
    if blobs_list.count > MAX_BLOBS_IN_TX {
        return Err(TxError::Validation(InvalidTransaction::BlobListTooLong));
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L423-436)
```rust
    #[cfg(feature = "eip-7702")]
    {
        if let Some(authorization_list) = transaction.authorization_list() {
            // Same as for the access list: gas is included in the intrinsic
            // gas above, so we are only charging native
            intrinsic_resources.with_infinite_ergs(|inf_resources| {
                crate::bootloader::transaction::authorization_list::parse_authorization_list_and_apply_delegations(
                    system,
                    inf_resources,
                    authorization_list,
                )
            })?;
        }
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L1-11)
```rust
use super::super::*;
use crate::bootloader::constants::{
    L1_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_CALLDATA_BYTE, L1_TX_INTRINSIC_NATIVE_COST,
    L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_ACCESS_LIST_PER_ADDRESS,
    L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_ACCESS_LIST_PER_STORAGE_KEY,
    L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_COST,
    L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION,
    L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_CALLDATA_BYTE, L2_TX_INTRINSIC_PUBDATA,
    L2_TX_INTRINSIC_PUBDATA_PER_AUTHORIZATION, SERVICE_TX_INTRINSIC_COMPUTATIONAL_NATIVE_COST,
    SERVICE_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_CALLDATA_BYTE, TX_INTRINSIC_GAS,
};
```
