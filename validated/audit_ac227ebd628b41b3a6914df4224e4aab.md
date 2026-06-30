### Title
Hardcoded `CUSTODIAN_ADDRESS` in `return_output` Becomes Stale on `eth_custodian` Redeployment, Enabling Forged Withdrawal Receipt Bypass - (File: `engine-sdk/src/near_runtime.rs`)

---

### Summary

`Runtime::return_output` in `engine-sdk/src/near_runtime.rs` contains a single hardcoded Ethereum address (`0x6BFaD42cFC4EfC96f529D786D643Ff4A8B89FA52`) as the sole guard against forged `WithdrawResult` payloads being returned from the Aurora `view` endpoint. If the Rainbow Bridge's `eth_custodian` contract is ever redeployed to a new address, this constant becomes stale and the guard is silently bypassed, allowing an attacker to forge a valid-looking `WithdrawResult` and drain ETH from the bridge.

---

### Finding Description

`Runtime::return_output` implements a security check introduced to fix GHSA-3p69-m8gg-fwmf (forged receipt exploit):

```rust
// engine-sdk/src/near_runtime.rs, lines 14-16
const CUSTODIAN_ADDRESS: &[u8] = &[
    107, 250, 212, 44, 252, 78, 252, 150, 245, 41, 215, 134, 214, 67, 255, 74, 139, 137, 250, 82,
];
``` [1](#0-0) 

```rust
// engine-sdk/src/near_runtime.rs, lines 265-273
fn return_output(&mut self, value: &[u8]) {
    unsafe {
        assert!(
            !(value.len() >= 56 && &value[36..56] == CUSTODIAN_ADDRESS),
            "ERR_ILLEGAL_RETURN"
        );
        exports::value_return(value.len() as u64, value.as_ptr() as u64);
    }
}
``` [2](#0-1) 

The check inspects bytes `[36..56]` of the return value against the hardcoded address. This corresponds exactly to the `eth_custodian_address` field in the borsh-serialized `WithdrawResult` struct:

```
WithdrawResult layout (56 bytes total):
  [0..16]  amount (NEP141Wei / u128)
  [16..36] recipient_id (Address / [u8;20])
  [36..56] eth_custodian_address (Address / [u8;20])  ← checked field
``` [3](#0-2) 

The code comment explicitly acknowledges the fragility: *"We use the address for the mainnet only, since for the testnet it's not so critical."* [4](#0-3) 

This is structurally identical to the ERC820 report's root cause: a single hardcoded external contract address used as a security invariant, with no mechanism to update it if the external contract migrates.

The existing regression test (`engine-tests/src/tests/ghsa_3p69_m8gg_fwmf.rs`) confirms the attack model: an attacker deploys an EVM echo contract, passes a crafted 56-byte payload containing the custodian address at offset 36, calls `view`, and the engine's `return_output` is expected to panic with `ERR_ILLEGAL_RETURN`. [5](#0-4) 

If `CUSTODIAN_ADDRESS` is stale (i.e., the bridge has migrated to a new `eth_custodian`), the `assert!` condition evaluates to `true` for the old address but `false` for the new one. The forged payload with the new address passes through `return_output` unchecked.

---

### Impact Explanation

**Critical — Direct theft of user funds (ETH) held in the Rainbow Bridge.**

A successful bypass produces a borsh-serialized `WithdrawResult` indistinguishable from a legitimate one. This forged receipt can be submitted to the Rainbow Bridge's Ethereum-side `eth_custodian` contract to claim ETH that was never actually withdrawn from Aurora, draining the bridge's ETH reserves. The `view` endpoint is callable by any unprivileged EVM user with no deposit or permission requirement.

---

### Likelihood Explanation

**Medium.** The Rainbow Bridge has already undergone contract upgrades (the CHANGES.md records multiple bridge-related security fixes). A redeployment of `eth_custodian` to a new Ethereum address — whether for an upgrade, a security patch, or a migration — is a realistic operational event. The code itself acknowledges the address is environment-specific and non-trivially coupled to an external contract. Unlike the ERC820 case where the standard was still in flux, here the trigger is an internal operational decision by the bridge team, making it a concrete rather than theoretical risk.

---

### Recommendation

1. Read the `eth_custodian_address` dynamically from the engine's on-chain connector state (where it is already stored as `InitCallArgs::eth_custodian_address`) rather than using a compile-time constant.
2. If a compile-time constant must be kept for gas reasons, add a runtime assertion at contract initialization that the stored connector address matches `CUSTODIAN_ADDRESS`, so a mismatch is caught at deploy time rather than silently ignored.
3. Add a governance-controlled method to update `CUSTODIAN_ADDRESS` in storage, mirroring how the ERC820 fix updated the registry address.

---

### Proof of Concept

**Pre-condition:** The Rainbow Bridge has redeployed `eth_custodian` to a new address, e.g. `0xDEADBEEF...` (20 bytes). `CUSTODIAN_ADDRESS` in the compiled Aurora Engine WASM still holds the old address.

1. Attacker deploys an EVM echo contract on Aurora (identical to the one in `ghsa_3p69_m8gg_fwmf.rs`).
2. Attacker crafts a 56-byte payload:
   - bytes `[0..16]`: any `u128` amount (e.g., `1_000_000 ETH` in wei)
   - bytes `[16..36]`: attacker's Ethereum address as recipient
   - bytes `[36..56]`: **new** `eth_custodian` address (`0xDEADBEEF...`)
3. Attacker calls `view` on the Aurora engine, targeting the echo contract with this payload as calldata.
4. The echo contract returns the 56-byte payload verbatim.
5. `return_output` checks `&value[36..56] == CUSTODIAN_ADDRESS` → **false** (old address ≠ new address) → `assert!` passes → forged `WithdrawResult` is returned.
6. Attacker submits the forged `WithdrawResult` to the Rainbow Bridge's Ethereum-side `eth_custodian` contract.
7. Bridge releases ETH to the attacker's address.

### Citations

**File:** engine-sdk/src/near_runtime.rs (L12-16)
```rust
/// The mainnet `eth_custodian` address 0x6BFaD42cFC4EfC96f529D786D643Ff4A8B89FA52. We use the
/// address for the mainnet only, since for the testnet it's not so critical.
const CUSTODIAN_ADDRESS: &[u8] = &[
    107, 250, 212, 44, 252, 78, 252, 150, 245, 41, 215, 134, 214, 67, 255, 74, 139, 137, 250, 82,
];
```

**File:** engine-sdk/src/near_runtime.rs (L265-273)
```rust
    fn return_output(&mut self, value: &[u8]) {
        unsafe {
            assert!(
                !(value.len() >= 56 && &value[36..56] == CUSTODIAN_ADDRESS),
                "ERR_ILLEGAL_RETURN"
            );
            exports::value_return(value.len() as u64, value.as_ptr() as u64);
        }
    }
```

**File:** engine-types/src/parameters/connector.rs (L167-172)
```rust
#[derive(BorshSerialize, BorshDeserialize)]
pub struct WithdrawResult {
    pub amount: NEP141Wei,
    pub recipient_id: Address,
    pub eth_custodian_address: Address,
}
```

**File:** engine-tests/src/tests/ghsa_3p69_m8gg_fwmf.rs (L1-43)
```rust
use crate::utils;

#[test]
fn test_exploit_fix() {
    let (mut runner, mut signer, _) = crate::tests::sanity::initialize_transfer();

    let constructor = utils::solidity::ContractConstructor::compile_from_source(
        "src/tests/res",
        "target/solidity_build",
        "echo.sol",
        "Echo",
    );

    let nonce = signer.use_nonce();
    let contract = runner.deploy_contract(
        &signer.secret_key,
        |c| c.deploy_without_constructor(nonce.into()),
        constructor,
    );

    let eth_custodian_address = "6bfad42cfc4efc96f529d786d643ff4a8b89fa52";
    let target_address = "1111111122222222333333334444444455555555";
    let amount: u64 = 1_000_000;
    let amount_bytes = amount.to_le_bytes();
    let payload = hex::decode(format!(
        "000000{}{}{}",
        hex::encode(amount_bytes),
        target_address,
        eth_custodian_address
    ))
    .unwrap();

    let tx = contract.call_method_with_args("echo", &[ethabi::Token::Bytes(payload)], nonce.into());
    let sender = utils::address_from_secret_key(&signer.secret_key);
    let view_call_args = utils::as_view_call(tx, sender);
    let input = borsh::to_vec(&view_call_args).unwrap();
    let error = runner.one_shot().call("view", "viewer", input).unwrap_err();

    assert!(
        error.kind.as_bytes().starts_with(b"ERR_ILLEGAL_RETURN"),
        "{error:?}"
    );
}
```
