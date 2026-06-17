### Title
`EthereumStorageModel::update_nominal_token_value` Silently Drops `fee_payment_in_simulation`, Causing Pubdata Underestimation Divergence Between Storage Model Implementations — (`File: basic_system/src/system_implementation/ethereum_storage_model/storage_model.rs`)

---

### Summary

The `StorageModel` trait defines `update_nominal_token_value` with a `fee_payment_in_simulation: bool` parameter that is supposed to mark balance updates caused by fee operations during simulation, enabling conservative pubdata estimation. The `FlatTreeWithAccountsUnderHashesStorageModel` correctly propagates this flag and sets the `not_compress_balance` metadata bit. The `EthereumStorageModel` implementation silently discards the parameter (prefixed `_fee_payment_in_simulation`), never setting any equivalent flag. This is a direct analog to the YetiToken bug: two implementations of the same interface operate on different internal state for the same logical operation, causing inconsistent behavior depending on which storage model is active.

---

### Finding Description

The `StorageModel` trait declares:

```rust
fn update_nominal_token_value(
    ...
    fee_payment_in_simulation: bool,
) -> Result<...>;
``` [1](#0-0) 

**Flat storage model** — correctly propagates the flag all the way to the cache entry, setting `not_compress_balance`:

```rust
fn update_nominal_token_value(..., fee_payment_in_simulation: bool, ...) {
    self.account_data_cache.update_nominal_token_value::<PROOF_ENV>(
        ..., fee_payment_in_simulation,
    )
}
``` [2](#0-1) 

Inside the flat cache's inner function, the flag sets a metadata bit:

```rust
m.not_compress_balance |= fee_payment_in_simulation;
``` [3](#0-2) 

The `not_compress_balance` flag is documented as:

> "Special flag to not compress balance diff for pubdata size estimation. It's used to have a conservative approximation of pubdata in simulation, when due to the gas price being set to 0 there might not be a diff." [4](#0-3) 

**Ethereum storage model** — silently discards the parameter (note the leading underscore):

```rust
fn update_nominal_token_value(
    ...
    _fee_payment_in_simulation: bool,   // ← DROPPED, never used
) -> Result<...> {
    self.account_cache
        .update_nominal_token_value::<PROOF_ENV>(from_ee, resources, address, update_fn, oracle)
        // fee_payment_in_simulation is NOT forwarded
}
``` [5](#0-4) 

The ethereum model's `update_nominal_token_value_inner` has no `fee_payment_in_simulation` parameter at all and no equivalent `not_compress_balance` concept: [6](#0-5) 

The callers — bootloader fee precharge, refund, and coinbase payment — all pass `Config::SIMULATION` as `fee_payment_in_simulation`: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

When the `EthereumStorageModel` is active and a transaction is simulated with `gas_price = 0`:

1. The bootloader calls `update_account_nominal_token_balance(..., fee_payment_in_simulation = true)` for fee-related balance changes.
2. The call reaches `EthereumStorageModel::update_nominal_token_value`, which drops the flag.
3. No `not_compress_balance` equivalent is set on the affected accounts.
4. Pubdata estimation for those balance diffs uses compressed encoding, producing a smaller pubdata estimate than actual execution would require.
5. A user whose transaction passes simulation (with the underestimated pubdata budget) may have their transaction fail in actual execution due to insufficient pubdata resources.

This is a **forward/proving divergence** and **resource accounting bug**: simulation underestimates pubdata, causing a class of transactions to be accepted by the sequencer's simulation path but rejected during actual block execution.

---

### Likelihood Explanation

Any L2 transaction submitted via simulation with `gas_price = 0` (the standard simulation pattern) that touches fee-related balance updates is affected when the `EthereumStorageModel` is the active storage backend. The entry path requires no privileged access — any unprivileged user submitting a transaction through the standard simulation RPC endpoint triggers this path.

---

### Recommendation

Remove the leading underscore from `_fee_payment_in_simulation` in `EthereumStorageModel::update_nominal_token_value` and implement the equivalent conservative pubdata estimation logic (analogous to `not_compress_balance`) within the ethereum account cache, or document explicitly why the ethereum model is exempt and enforce that it is never used in simulation mode.

---

### Proof of Concept

1. Configure ZKsync OS to use `EthereumStorageModel` as the active storage backend.
2. Submit a simulation request for a transaction with `gas_price = 0` and a non-trivial fee budget (e.g., `gas_limit = 100_000`, `gas_price = 0`).
3. Observe that `update_nominal_token_value` is called with `fee_payment_in_simulation = true` for the fee precharge and refund balance updates.
4. Confirm via the ethereum model's code path that `_fee_payment_in_simulation` is discarded and no `not_compress_balance`-equivalent flag is set.
5. Re-run the same transaction with `gas_price > 0` in actual execution mode; the pubdata charged for the same balance diffs will be larger (uncompressed), causing the transaction to exceed its pubdata budget and revert — a divergence not predicted by simulation. [5](#0-4) [9](#0-8) [10](#0-9)

### Citations

**File:** storage_models/src/common_structs/traits/storage_model.rs (L127-144)
```rust
    /// Updates the nominal token balance for an address using the provided update function.
    fn update_nominal_token_value(
        &mut self,
        from_ee: ExecutionEnvironmentType,
        resources: &mut Self::Resources,
        address: &<Self::IOTypes as SystemIOTypesConfig>::Address,
        update_fn: impl FnOnce(
            &<Self::IOTypes as SystemIOTypesConfig>::NominalTokenValue,
        ) -> Result<
            <Self::IOTypes as SystemIOTypesConfig>::NominalTokenValue,
            BalanceSubsystemError,
        >,
        oracle: &mut impl IOOracle,
        fee_payment_in_simulation: bool,
    ) -> Result<
        <Self::IOTypes as zk_ee::types_config::SystemIOTypesConfig>::NominalTokenValue,
        BalanceSubsystemError,
    >;
```

**File:** basic_system/src/system_implementation/flat_storage_model/mod.rs (L382-408)
```rust
    fn update_nominal_token_value(
        &mut self,
        from_ee: ExecutionEnvironmentType,
        resources: &mut Self::Resources,
        address: &<Self::IOTypes as SystemIOTypesConfig>::Address,
        update_fn: impl FnOnce(
            &<Self::IOTypes as SystemIOTypesConfig>::NominalTokenValue,
        ) -> Result<
            <Self::IOTypes as SystemIOTypesConfig>::NominalTokenValue,
            BalanceSubsystemError,
        >,
        oracle: &mut impl IOOracle,
        fee_payment_in_simulation: bool,
    ) -> Result<<Self::IOTypes as SystemIOTypesConfig>::NominalTokenValue, BalanceSubsystemError>
    {
        self.account_data_cache
            .update_nominal_token_value::<PROOF_ENV>(
                from_ee,
                resources,
                address,
                update_fn,
                &mut self.storage_cache,
                &mut self.preimages_cache,
                oracle,
                fee_payment_in_simulation,
            )
    }
```

**File:** basic_system/src/system_implementation/flat_storage_model/account_cache.rs (L56-68)
```rust
/// Extension of basic properties
#[derive(Default, Clone)]
pub struct AccountPropertiesMetadata {
    pub basic: BasicAccountPropertiesMetadata,
    /// Special flag that allows avoiding publishing bytecode for deployed account.
    /// In practice, it can be set to `true` only during special protocol upgrade txs.
    /// For protocol upgrades it's ensured by governance that bytecodes are already published separately.
    pub not_publish_bytecode: bool,
    /// Special flag to not compress balance diff for pubdata size estimation.
    /// It's used to have a conservative approximation of pubdata in simulation,
    /// when due to the gas price being set to 0 there might not be a diff.
    pub not_compress_balance: bool,
}
```

**File:** basic_system/src/system_implementation/flat_storage_model/account_cache.rs (L299-339)
```rust
    fn update_nominal_token_value_inner<const PROOF_ENV: bool>(
        &mut self,
        ee_type: ExecutionEnvironmentType,
        resources: &mut R,
        address: &B160,
        update_fn: impl FnOnce(&U256) -> Result<U256, BalanceSubsystemError>,
        storage: &mut NewStorageWithAccountPropertiesUnderHash<A, SF, M, R, P>,
        preimages_cache: &mut impl PreimageCacheModel<Resources = R, PreimageRequest = PreimageRequest>,
        oracle: &mut impl IOOracle,
        is_selfdestruct: bool,
        fee_payment_in_simulation: bool,
    ) -> Result<U256, BalanceSubsystemError> {
        let mut account_data = self.materialize_element::<PROOF_ENV>(
            ee_type,
            resources,
            address,
            storage,
            preimages_cache,
            oracle,
            is_selfdestruct,
            true,
        )?;

        resources.charge(&R::from_native(R::Native::from_computational(
            WARM_ACCOUNT_CACHE_WRITE_EXTRA_NATIVE_COST,
        )))?;

        let cur = account_data.current().value().balance;
        let new = update_fn(&cur)?;
        account_data.update(|cache_record| {
            cache_record.update(|v, m| {
                v.balance = new;
                // Once an account's balance has been affected by fee
                // payment, we keep this flag set.
                m.not_compress_balance |= fee_payment_in_simulation;
                Ok(())
            })
        })?;

        Ok(cur)
    }
```

**File:** basic_system/src/system_implementation/ethereum_storage_model/storage_model.rs (L326-343)
```rust
    fn update_nominal_token_value(
        &mut self,
        from_ee: ExecutionEnvironmentType,
        resources: &mut Self::Resources,
        address: &<Self::IOTypes as SystemIOTypesConfig>::Address,
        update_fn: impl FnOnce(
            &<Self::IOTypes as SystemIOTypesConfig>::NominalTokenValue,
        ) -> Result<
            <Self::IOTypes as SystemIOTypesConfig>::NominalTokenValue,
            BalanceSubsystemError,
        >,
        oracle: &mut impl IOOracle,
        _fee_payment_in_simulation: bool,
    ) -> Result<<Self::IOTypes as SystemIOTypesConfig>::NominalTokenValue, BalanceSubsystemError>
    {
        self.account_cache
            .update_nominal_token_value::<PROOF_ENV>(from_ee, resources, address, update_fn, oracle)
    }
```

**File:** basic_system/src/system_implementation/ethereum_storage_model/caches/account_cache.rs (L200-235)
```rust
    fn update_nominal_token_value_inner<const PROOF_ENV: bool>(
        &mut self,
        ee_type: ExecutionEnvironmentType,
        resources: &mut R,
        address: &B160,
        update_fn: impl FnOnce(&U256) -> Result<U256, BalanceSubsystemError>,
        oracle: &mut impl IOOracle,
        is_selfdestruct: bool,
    ) -> Result<U256, BalanceSubsystemError> {
        let mut account_data = self.materialize_element::<PROOF_ENV>(
            ee_type,
            resources,
            address,
            oracle,
            is_selfdestruct,
            false,
        )?;

        resources.charge(&R::from_native(R::Native::from_computational(
            WARM_ACCOUNT_CACHE_WRITE_EXTRA_NATIVE_COST,
        )))?;

        let cur = account_data.current().value().balance;
        let new = update_fn(&cur)?;
        account_data
            .element_properties_mut()
            .mark_value_as_observed();
        account_data.update(|cache_record| {
            cache_record.update(|v, _| {
                v.balance = new;
                Ok(())
            })
        })?;

        Ok(cur)
    }
```
