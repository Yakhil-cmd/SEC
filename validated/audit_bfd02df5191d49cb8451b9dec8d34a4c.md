### Title
Zero EVM Gas Cost on `ExitToNear` and `ExitToEthereum` Precompiles Allows Spam of Expensive NEAR Cross-Contract Calls - (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToNear` and `ExitToEthereum` bridge precompiles have their EVM gas costs hardcoded to `0`, despite each invocation triggering NEAR cross-contract calls that consume 10T–100T NEAR gas. Any unprivileged EVM user can spam these precompiles at near-zero EVM cost, exhausting the NEAR gas budget of the Aurora contract and causing temporary denial of service on bridge withdrawals.

---

### Finding Description

In `engine-precompiles/src/native.rs`, both bridge precompiles declare their EVM gas cost as zero:

```rust
// TODO(#483): Determine the correct amount of gas
pub(super) const EXIT_TO_NEAR_GAS: EthGas = EthGas::new(0);

// TODO(#483): Determine the correct amount of gas
pub(super) const EXIT_TO_ETHEREUM_GAS: EthGas = EthGas::new(0);
``` [1](#0-0) 

These costs are returned directly from `required_gas` and charged to the EVM caller:

```rust
impl<I: IO> Precompile for ExitToNear<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_NEAR_GAS)  // returns 0
    }
``` [2](#0-1) 

```rust
impl<I: IO> Precompile for ExitToEthereum<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_ETHEREUM_GAS)  // returns 0
    }
``` [3](#0-2) 

Yet each successful invocation creates NEAR promises with substantial attached gas:

- `ExitToNear` → `ft_transfer`: **10T NEAR gas** (`FT_TRANSFER_GAS`)
- `ExitToNear` → `ft_transfer_call`: **70T NEAR gas** (`FT_TRANSFER_CALL_GAS`)
- `ExitToNear` → `near_withdraw` + callback: **100T + 10T NEAR gas** (`WITHDRAWAL_GAS + EXIT_TO_NEAR_CALLBACK_GAS`)
- `ExitToEthereum` → `withdraw`: **100T NEAR gas** (`WITHDRAWAL_GAS`) [4](#0-3) 

The NEAR gas consumed by these promises is paid from the Aurora contract's NEAR gas budget (the NEAR transaction wrapping the EVM call), not from the EVM caller's ETH balance. The EVM caller only pays the base transaction cost of 21,000 EVM gas.

Critically, for the ETH base-token path of `ExitToNear`, there is **no minimum value check** on `context.apparent_value`. An attacker can call the precompile with `value = 0 ETH`, and the NEAR promise (`ft_transfer` for 0 amount) is still created and dispatched, consuming 10T–70T NEAR gas per call:

```rust
ExitToNearParams::BaseToken(ref exit_params) => {
    let eth_connector_account_id = self.get_eth_connector_contract_account()?;
    exit_base_token_to_near(eth_connector_account_id, context, exit_params)?
}
``` [5](#0-4) 

The `CrossContractCall` precompile, by contrast, correctly accounts for NEAR gas in its EVM cost:

```rust
cost += EthGas::new(promise.attached_gas.as_u64() / costs::CROSS_CONTRACT_CALL_NEAR_GAS);
``` [6](#0-5) 

The `ExitToNear` and `ExitToEthereum` precompiles have no equivalent accounting.

---

### Impact Explanation

**High — Temporary freezing of funds.**

An attacker can submit a flood of EVM transactions calling `ExitToNear` (or `ExitToEthereum`) with 0 ETH value. Each transaction costs only the base 21,000 EVM gas (payable in ETH at whatever gas price is set), but forces the Aurora NEAR contract to dispatch NEAR cross-contract calls consuming 10T–100T NEAR gas each. This exhausts the NEAR gas budget available to the Aurora contract per block, causing legitimate bridge withdrawal transactions to fail or be delayed. Funds in transit (pending bridge withdrawals) are temporarily frozen until the spam subsides.

---

### Likelihood Explanation

**High.** The attack requires no special privileges — any EVM address with a trivial ETH balance (enough to pay 21,000 EVM gas at any gas price) can execute it. The entry path is a standard EVM `call` to the precompile address `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` (`ExitToNear`) or `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab` (`ExitToEthereum`). The `TODO(#483)` comments confirm the zero costs are placeholders, not intentional design.

---

### Recommendation

Apply the same methodology used in the `CrossContractCall` precompile: compute the EVM gas cost as a function of the NEAR gas that will be consumed by the dispatched promise, using the `CROSS_CONTRACT_CALL_NEAR_GAS` conversion ratio (`175_000_000 NEAR gas / EVM gas`). Additionally, add a minimum-value guard on the base-token ETH path to reject calls with `apparent_value == 0`. [7](#0-6) 

---

### Proof of Concept

1. Deploy a contract on Aurora (or use an EOA) with any non-zero ETH balance.
2. Craft an EVM transaction calling `ExitToNear` at address `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` with:
   - `value = 0`
   - `data = 0x00 || <valid_near_account_id_bytes>` (flag 0x00 = base token, followed by a valid NEAR account ID)
   - `gas_limit = 21100` (just above base cost; precompile charges 0 additional gas)
3. Repeat in a loop. Each call dispatches a `ft_transfer` NEAR promise consuming 10T NEAR gas at zero incremental EVM gas cost to the attacker.
4. Observe that legitimate bridge withdrawal transactions begin failing as the Aurora contract's NEAR gas budget is saturated. [8](#0-7)

### Citations

**File:** engine-precompiles/src/native.rs (L45-49)
```rust
    // TODO(#483): Determine the correct amount of gas
    pub(super) const EXIT_TO_NEAR_GAS: EthGas = EthGas::new(0);

    // TODO(#483): Determine the correct amount of gas
    pub(super) const EXIT_TO_ETHEREUM_GAS: EthGas = EthGas::new(0);
```

**File:** engine-precompiles/src/native.rs (L53-61)
```rust
    pub(super) const FT_TRANSFER_GAS: NearGas = NearGas::new(10_000_000_000_000);

    pub(super) const FT_TRANSFER_CALL_GAS: NearGas = NearGas::new(70_000_000_000_000);

    /// Value determined experimentally based on tests.
    pub(super) const EXIT_TO_NEAR_CALLBACK_GAS: NearGas = NearGas::new(10_000_000_000_000);

    // TODO(#332): Determine the correct amount of gas
    pub(super) const WITHDRAWAL_GAS: NearGas = NearGas::new(100_000_000_000_000);
```

**File:** engine-precompiles/src/native.rs (L381-384)
```rust
impl<I: IO> Precompile for ExitToNear<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_NEAR_GAS)
    }
```

**File:** engine-precompiles/src/native.rs (L386-410)
```rust
    #[allow(clippy::too_many_lines)]
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        context: &Context,
        is_static: bool,
    ) -> EvmPrecompileResult {
        // ETH (base) transfer input format: (85 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled
        //  - recipient_account_id (max MAX_INPUT_SIZE - 20 - 1 bytes)
        // ERC-20 transfer input format: (124 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled.
        //  - amount (32 bytes)
        //  - recipient_account_id (max MAX_INPUT_SIZE - 1 - (20) - 32 bytes)
        //  - `:unwrap` suffix in a case of wNEAR (7 bytes)
        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }
```

**File:** engine-precompiles/src/native.rs (L430-433)
```rust
                ExitToNearParams::BaseToken(ref exit_params) => {
                    let eth_connector_account_id = self.get_eth_connector_contract_account()?;
                    exit_base_token_to_near(eth_connector_account_id, context, exit_params)?
                }
```

**File:** engine-precompiles/src/native.rs (L844-847)
```rust
impl<I: IO> Precompile for ExitToEthereum<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_ETHEREUM_GAS)
    }
```

**File:** engine-precompiles/src/xcc.rs (L43-45)
```rust
    /// The report gives a value `0.175 T(NEAR_gas) / k(EVM_gas)`. To convert the units to
    /// `NEAR Gas / EVM Gas`, we simply multiply `0.175 * 10^12 / 10^3 = 175 * 10^6`.
    pub const CROSS_CONTRACT_CALL_NEAR_GAS: u64 = 175_000_000;
```

**File:** engine-precompiles/src/xcc.rs (L174-175)
```rust
        cost += EthGas::new(promise.attached_gas.as_u64() / costs::CROSS_CONTRACT_CALL_NEAR_GAS);
        check_cost(cost)?;
```
