### Title
Unbounded Gas Refund Accumulation via Repeated EIP-7702 Authorization Entries for the Same Authority - (File: `basic_bootloader/src/bootloader/transaction/authorization_list.rs`)

### Summary
The `parse_authorization_list_and_apply_delegations` function in the ZKsync OS bootloader processes each EIP-7702 authorization entry independently without cross-entry validation. An attacker can craft a single EIP-7702 transaction whose authorization list contains multiple entries that resolve to the **same authority address**, each with a sequentially increasing nonce. Because the nonce is bumped after each successful delegation (step 9), and the `is_empty` check at step 7 reads the **live (post-write) account state**, the authority account will appear non-empty on every successive entry, causing the gas refund counter to be incremented once per entry. This allows an attacker to accumulate an arbitrarily large gas refund (capped only by 1/5 of gas used) by padding the authorization list with many entries for the same authority.

### Finding Description

In `parse_authorization_list_and_apply_delegations`, the outer loop iterates over every `AuthorizationEntry` in the list and calls `validate_and_apply_delegation` for each one independently:

```
for entry in auth_list.iter() {
    let success = validate_and_apply_delegation(...)?;
}
``` [1](#0-0) 

Inside `validate_and_apply_delegation`, after a successful delegation is applied, the nonce of the authority is incremented by 1 (step 9):

```rust
system.io.increment_nonce(ExecutionEnvironmentType::NoEE, inf_ergs, &authority, 1)
``` [2](#0-1) 

Before that, step 7 checks whether the authority account is "empty" and, if it is **not** empty, adds a gas refund to the refund counter:

```rust
let is_empty = account_properties.nonce.0 == 0
    && account_properties.has_bytecode() == false
    && account_properties.nominal_token_balance.0.is_zero();

if !is_empty {
    let ergs = Ergs(
        (evm_interpreter::gas_constants::NEWACCOUNT
            - evm_interpreter::gas_constants::PER_AUTH_BASE_COST)
            * ERGS_PER_GAS,
    );
    system.io.add_to_refund_counter(S::Resources::from_ergs(ergs))?
}
``` [3](#0-2) 

The `is_contract()` check (step 5) only rejects accounts that have bytecode **and** are not delegated:

```rust
pub fn is_contract(&self) -> bool {
    self.has_bytecode() && self.is_delegated.0 == false
}
``` [4](#0-3) 

**Attack sequence:**

1. Attacker controls an EOA `A` with nonce `N` and a non-zero balance (making it non-empty).
2. Attacker crafts an EIP-7702 transaction with an authorization list containing `K` entries, all signed by `A`, with nonces `N, N+1, N+2, ..., N+K-1`.
3. Entry 0 is processed: nonce matches `N`, `A` is non-empty → refund added, nonce bumped to `N+1`, delegation set.
4. Entry 1 is processed: nonce now matches `N+1`, `A` still has non-zero balance → `is_empty` is false → refund added again, nonce bumped to `N+2`.
5. This repeats for all `K` entries, accumulating `K` refund increments.

The `is_empty` check uses the **live** account state (read from the cache after the previous iteration's nonce bump and delegation write), so the account will never appear empty as long as it holds a non-zero balance. There is no cross-entry deduplication or limit on how many times the same authority can appear.

The refund is capped at 1/5 of gas used by `compute_gas_refund`:

```rust
let max_refund = gas_used / 5;
core::cmp::min(full_refund_gas, max_refund)
``` [5](#0-4) 

However, the attacker can set a high gas limit to maximize the 1/5 cap, and the intrinsic gas cost per authorization entry (`NEWACCOUNT * ERGS_PER_GAS`) is charged upfront, so the attacker pays for each entry. The net effect is that the attacker can systematically reduce their effective gas cost by the maximum 1/5 refund on every EIP-7702 transaction, by simply padding the authorization list with many self-referential entries.

### Impact Explanation

An unprivileged transaction sender can craft EIP-7702 transactions that always extract the maximum 1/5 gas refund, regardless of whether the transaction's execution logic would normally warrant any refund. This is a **resource accounting bug**: the gas accounting model is violated because refunds are granted for each repeated authorization entry for the same authority, not just once. Over many transactions, this allows an attacker to pay ~17% less gas than they should, effectively subsidizing their transaction costs at the expense of the block's fee model integrity. In a ZK rollup context, this also means the prover's resource accounting diverges from the expected model, potentially causing state-transition inconsistencies between forward execution and proof verification.

### Likelihood Explanation

The attack requires only a valid EOA with a non-zero balance and the ability to submit EIP-7702 transactions. No privileged access is needed. The attacker pre-signs multiple authorization entries for their own account with sequential nonces, which is straightforward. The `eip-7702` feature must be enabled, but it is present in the codebase and tested. [6](#0-5) 

### Recommendation

Add a cross-entry validation step in `parse_authorization_list_and_apply_delegations` to detect and reject (or skip) duplicate authority addresses within the same authorization list. Specifically, before calling `validate_and_apply_delegation`, check whether the recovered authority address has already been processed in the current list. If it has, skip the entry (return `Ok(false)`) without adding a refund. This mirrors the analog fix for the Velodrome route validation: validate the sequence of entries for internal consistency, not just each entry in isolation.

Alternatively, the refund for a non-empty authority should only be granted once per authority per transaction, tracked via a seen-authorities set.

### Proof of Concept

1. Deploy EOA `A` with nonce `0` and balance `1 ETH`.
2. Pre-sign `K = 100` authorization entries, all for authority `A`, with nonces `0, 1, 2, ..., 99`, each delegating to some contract address `C`.
3. Submit an EIP-7702 transaction from any sender with `gas_limit = G` (large), containing all 100 entries.
4. The bootloader processes each entry:
   - Entry `i`: nonce check passes (`A.nonce == i`), `A` has non-zero balance → `is_empty == false` → `add_to_refund_counter(NEWACCOUNT - PER_AUTH_BASE_COST)` is called → nonce bumped to `i+1`.
5. After processing all 100 entries, the refund counter holds `100 * (NEWACCOUNT - PER_AUTH_BASE_COST) * ERGS_PER_GAS`.
6. At refund calculation time, `evm_refund = min(full_refund_gas, gas_used / 5)`, so the attacker receives the maximum 1/5 refund.
7. The attacker has paid intrinsic cost for 100 entries but receives a disproportionate refund, reducing their effective gas cost to the minimum allowed by EIP-3529. [7](#0-6) [8](#0-7)

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

**File:** basic_bootloader/src/bootloader/transaction/authorization_list.rs (L85-207)
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

    // 4. Read authority account
    // Gas already charged in intrinsic
    let account_properties = resources.with_infinite_ergs(|inf_ergs| {
        system.io.read_account_properties(
            ExecutionEnvironmentType::NoEE,
            inf_ergs,
            &authority,
            AccountDataRequest::empty()
                .with_nonce()
                .with_nominal_token_balance()
                .with_is_delegated()
                .with_has_bytecode(),
        )
    })?;
    // 5. Check authority is not a contract
    if account_properties.is_contract() {
        return Ok(false);
    }
    // 6. Check nonce
    if account_properties.nonce.0 != auth_nonce {
        return Ok(false);
    }
    // 7. Add refund if authority is not empty.
    let is_empty = account_properties.nonce.0 == 0
        && account_properties.has_bytecode() == false
        && account_properties.nominal_token_balance.0.is_zero();

    if !is_empty {
        let ergs = Ergs(
            (evm_interpreter::gas_constants::NEWACCOUNT
                - evm_interpreter::gas_constants::PER_AUTH_BASE_COST)
                * ERGS_PER_GAS,
        );
        system
            .io
            .add_to_refund_counter(S::Resources::from_ergs(ergs))?
    }

    let delegation_address = B160::from_be_bytes(*delegation_address);
    system_log!(
        system,
        "Will delegate address 0x{:040x} -> 0x{:040x}\n",
        authority.as_uint(),
        delegation_address.as_uint()
    );

    // 8. Set code for authority, system function
    //    will handle the two cases (unsetting).
    resources.with_infinite_ergs(|inf_ergs| {
        system
            .io
            .set_delegation(inf_ergs, &authority, &delegation_address)
    })?;
    // 9.Bump nonce
    resources
        .with_infinite_ergs(|inf_ergs| {
            system
                .io
                .increment_nonce(ExecutionEnvironmentType::NoEE, inf_ergs, &authority, 1)
        })
        .map_err(|e| -> BootloaderSubsystemError {
            match e {
                SubsystemError::LeafUsage(InterfaceError(NonceError::NonceOverflow, _)) => {
                    internal_error!("Cannot overflow, already checked").into()
                }
                _ => wrap_error!(e),
            }
        })?;
    Ok(true)
}
```

**File:** zk_ee/src/system/io.rs (L258-261)
```rust
impl<A, B, D, E, F, G, H, I, J> AccountData<A, B, Just<u32>, D, E, F, G, H, I, J, Just<bool>> {
    pub fn is_contract(&self) -> bool {
        self.has_bytecode() && self.is_delegated.0 == false
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L39-44)
```rust
    let evm_refund = {
        let full_refund_ergs = system.io.get_refund_counter().ergs();
        let full_refund_gas = full_refund_ergs.0.div_floor(ERGS_PER_GAS);
        let max_refund = gas_used / 5;
        core::cmp::min(full_refund_gas, max_refund)
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs (L347-353)
```rust
    if let Some(auth_list) = transaction.authorization_list() {
        parse_authorization_list_and_apply_delegations(
            system,
            &mut tx_resources.main_resources,
            auth_list,
        )?
    }
```
