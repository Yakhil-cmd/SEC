### Title
Zero EVM Gas Cost for `ExitToNear` and `ExitToEthereum` Precompiles Causes Operator Insolvency - (File: `engine-precompiles/src/native.rs`)

### Summary
The `ExitToNear` and `ExitToEthereum` bridge exit precompiles charge **0 EVM gas** to callers, yet each invocation unconditionally creates NEAR cross-contract call promises that consume 10–100 TGas of real NEAR gas paid by the Aurora operator. Any unprivileged EVM user can repeatedly invoke these precompiles at near-zero cost, forcing the operator to spend NEAR gas far in excess of collected EVM fees, leading to operator insolvency.

### Finding Description
In `engine-precompiles/src/native.rs`, the EVM gas costs for both bridge exit precompiles are hardcoded to zero with an unresolved TODO:

```rust
// TODO(#483): Determine the correct amount of gas
pub(super) const EXIT_TO_NEAR_GAS: EthGas = EthGas::new(0);

// TODO(#483): Determine the correct amount of gas
pub(super) const EXIT_TO_ETHEREUM_GAS: EthGas = EthGas::new(0);
``` [1](#0-0) 

Despite charging 0 EVM gas, each precompile invocation unconditionally constructs and emits a NEAR promise with substantial attached gas:

- `ExitToNear` → `ft_transfer`: `FT_TRANSFER_GAS = 10_000_000_000_000` (10 TGas)
- `ExitToNear` → `ft_transfer_call`: `FT_TRANSFER_CALL_GAS = 70_000_000_000_000` (70 TGas)
- `ExitToEthereum` → `withdraw`: `WITHDRAWAL_GAS = 100_000_000_000_000` (100 TGas) [2](#0-1) 

The promise is created unconditionally regardless of the transferred amount:

```rust
let transfer_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method,
    args: args.into_bytes(),
    attached_balance: Yocto::new(1),
    attached_gas,  // 10–100 TGas
};
``` [3](#0-2) 

The `ExitToNear::required_gas` and `ExitToEthereum::required_gas` both return `EthGas::new(0)`, so the EVM executor charges the caller nothing for the precompile invocation: [4](#0-3) [5](#0-4) 

The NEAR gas attached to these promises is paid by the Aurora operator (relayer), not by the EVM user. There is no minimum-amount guard on the base-token path of `ExitToNear`; a user can supply 0 ETH value and still trigger the full NEAR cross-contract call.

The XCC precompile, by contrast, correctly converts NEAR gas cost to EVM gas using `CROSS_CONTRACT_CALL_NEAR_GAS = 175_000_000`: [6](#0-5) 

The exit precompiles apply no equivalent conversion, leaving the NEAR gas cost entirely unrecovered.

### Impact Explanation
The Aurora operator pays real NEAR gas for every precompile invocation while collecting 0 EVM gas from the caller. At NEAR's gas price (~0.0001 NEAR/TGas):

| Precompile | NEAR gas cost | Operator cost (at $3/NEAR) | User EVM cost (at 0.07 gwei, 21k gas) |
|---|---|---|---|
| ExitToNear (ft_transfer) | 10 TGas | ~$0.003 | ~$0.000044 |
| ExitToNear (ft_transfer_call) | 70 TGas | ~$0.021 | ~$0.000044 |
| ExitToEthereum (withdraw) | 100 TGas | ~$0.030 | ~$0.000044 |

The operator pays 68–682× more than the user. An attacker can loop these calls indefinitely, draining the operator's NEAR balance. This directly matches the insolvency impact class: the chain becomes uneconomical to operate, analogous to the `commitScalar` 100× underpricing in the reference report.

### Likelihood Explanation
The entry path requires no special privileges. Any EVM account can call `ExitToNear` at address `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` directly with a 0-ETH base-token transfer and a valid NEAR account ID as recipient. No tokens need to be held. The attack is repeatable in every block. The TODO comment confirms the zero cost is an unresolved placeholder, not an intentional design choice.

### Recommendation
Apply the same NEAR-gas-to-EVM-gas conversion used by the XCC precompile (`CROSS_CONTRACT_CALL_NEAR_GAS = 175_000_000`) to derive correct floor values:

- `EXIT_TO_NEAR_GAS` ≥ `FT_TRANSFER_CALL_GAS / CROSS_CONTRACT_CALL_NEAR_GAS` = 70 TGas / 175,000,000 ≈ **400,000 EVM gas**
- `EXIT_TO_ETHEREUM_GAS` ≥ `WITHDRAWAL_GAS / CROSS_CONTRACT_CALL_NEAR_GAS` = 100 TGas / 175,000,000 ≈ **571,428 EVM gas**

Additionally, add a minimum-amount guard on the base-token path of `ExitToNear` to reject 0-value transfers before any promise is created.

### Proof of Concept
1. Attacker calls `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) with:
   - Input: `0x00` (flag = base token) + any valid NEAR account ID bytes
   - ETH value: 0
   - Gas limit: 21,000 (minimum)
2. `required_gas` returns 0; the EVM executor charges the caller 0 EVM gas for the precompile.
3. The precompile constructs a `PromiseCreateArgs` targeting the ETH connector with `ft_transfer` and `attached_gas = FT_TRANSFER_GAS = 10 TGas`.
4. The promise log is emitted; the Aurora engine's NEAR transaction executes the promise, consuming 10 TGas from the operator's prepaid gas.
5. The `ft_transfer` call fails on the NEAR side (amount = 0 violates NEP-141), but NEAR gas is consumed regardless.
6. Operator net loss per iteration: ~0.001 NEAR. User net cost: ~21,000 × gas_price (negligible).
7. Repeat in a loop across blocks to drain the operator's NEAR balance and render the chain uneconomical.

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

**File:** engine-precompiles/src/native.rs (L462-468)
```rust
        let transfer_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method,
            args: args.into_bytes(),
            attached_balance: Yocto::new(1),
            attached_gas,
        };
```

**File:** engine-precompiles/src/native.rs (L844-847)
```rust
impl<I: IO> Precompile for ExitToEthereum<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_ETHEREUM_GAS)
    }
```

**File:** engine-precompiles/src/xcc.rs (L40-45)
```rust
    /// EVM gas cost per NEAR gas attached to the created promise.
    /// This value is derived from the gas report `https://hackmd.io/@birchmd/Sy4piXQ29`
    /// The units on this quantity are `NEAR Gas / EVM Gas`.
    /// The report gives a value `0.175 T(NEAR_gas) / k(EVM_gas)`. To convert the units to
    /// `NEAR Gas / EVM Gas`, we simply multiply `0.175 * 10^12 / 10^3 = 175 * 10^6`.
    pub const CROSS_CONTRACT_CALL_NEAR_GAS: u64 = 175_000_000;
```
