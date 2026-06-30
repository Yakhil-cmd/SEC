### Title
Hardcoded `CUSTODIAN_ADDRESS` in `return_output` Becomes Stale After Ethereum Custodian Upgrade - (File: engine-sdk/src/near_runtime.rs)

### Summary
The Aurora Engine's `return_output` function in `Runtime` contains a hardcoded compile-time constant `CUSTODIAN_ADDRESS` (`0x6BFaD42cFC4EfC96f529D786D643Ff4A8B89FA52`) used as a security guard against a known exploit (GHSA-3p69-m8gg-fwmf). If the Ethereum-side custodian contract is ever upgraded to a new address and the eth-connector's `eth_custodian_address` is updated via `set_eth_connector_contract_data`, the hardcoded check becomes stale. A malicious EVM contract can then craft a return value containing the new custodian address at the expected byte offset, bypassing the guard and allowing the relayer to process it as a legitimate `WithdrawResult`, leading to direct theft of bridged ETH.

### Finding Description

The `return_output` implementation for the NEAR `Runtime` contains a hardcoded 20-byte constant:

```rust
// engine-sdk/src/near_runtime.rs, lines 12-16
/// The mainnet `eth_custodian` address 0x6BFaD42cFC4EfC96f529D786D643Ff4A8B89FA52. We use the
/// address for the mainnet only, since for the testnet it's not so critical.
const CUSTODIAN_ADDRESS: &[u8] = &[
    107, 250, 212, 44, 252, 78, 252, 150, 245, 41, 215, 134, 214, 67, 255, 74, 139, 137, 250, 82,
];
``` [1](#0-0) 

This constant is used in `return_output` to block any EVM contract from returning data that structurally matches a Borsh-serialized `WithdrawResult`:

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

The byte offsets `[36..56]` correspond exactly to the `eth_custodian_address` field in `WithdrawResult`, whose Borsh layout is:

- `amount: NEP141Wei` → 16 bytes (u128)
- `recipient_id: Address` → 20 bytes
- `eth_custodian_address: Address` → 20 bytes (bytes 36–55) [3](#0-2) 

The eth-connector's Ethereum-side custodian address is **not** hardcoded in the engine state — it is configurable via `InitCallArgs.eth_custodian_address` / `SetContractDataCallArgs`: [4](#0-3) 

If the Ethereum custodian contract is upgraded and the eth-connector's stored `eth_custodian_address` is updated via `set_eth_connector_contract_data`, the Aurora Engine WASM binary still contains the old hardcoded `CUSTODIAN_ADDRESS`. The check in `return_output` will no longer match the new custodian address, leaving the guard ineffective.

### Impact Explanation

**Critical — Direct theft of bridged ETH.**

The `ERR_ILLEGAL_RETURN` guard was introduced specifically to prevent a class of exploit where a malicious EVM contract, called via Aurora's `view` method, returns bytes that structurally match a `WithdrawResult`. The NEAR relayer interprets this return value as a legitimate withdrawal proof and processes it, crediting the attacker with ETH on Ethereum without a corresponding burn on Aurora.

Once `CUSTODIAN_ADDRESS` is stale (i.e., the real custodian address has changed), any unprivileged EVM user can deploy a contract that echoes back a crafted 56-byte payload with the new custodian address at bytes `[36..56]`. The `return_output` check passes, the relayer processes the fake `WithdrawResult`, and the attacker receives ETH from the bridge custodian without having burned any tokens on Aurora.

### Likelihood Explanation

**Medium.** The Ethereum custodian contract has been deployed at a fixed address for a long time, but custodian upgrades are a realistic operational event (security patches, feature upgrades). The comment in the source code itself acknowledges the address is environment-specific: *"We use the address for the mainnet only, since for the testnet it's not so critical."* This confirms the developers are aware the address can differ across deployments, yet no mechanism exists to keep `CUSTODIAN_ADDRESS` synchronized with the live custodian address without a full WASM redeployment. Any governance action that updates the custodian address without simultaneously redeploying the engine binary opens the window.

### Recommendation

Replace the compile-time constant `CUSTODIAN_ADDRESS` with a runtime lookup that reads the current `eth_custodian_address` from the eth-connector's on-chain state (analogous to how `get_eth_connector_contract_account` reads the connector account ID from storage). The `return_output` guard should compare against the dynamically fetched address so it remains valid across custodian upgrades. Alternatively, maintain a set of all historical custodian addresses and check against all of them, mirroring the recommendation in the original report.

### Proof of Concept

1. The current mainnet custodian address `0x6BFaD42cFC4EfC96f529D786D643Ff4A8B89FA52` is hardcoded at compile time. [5](#0-4) 

2. The eth-connector's custodian address is updated on-chain via `set_eth_connector_contract_data` (using `SetContractDataCallArgs = InitCallArgs` with a new `eth_custodian_address`). The Aurora Engine WASM binary is **not** redeployed. [4](#0-3) 

3. Attacker deploys a malicious EVM contract (analogous to the `Echo` contract in `ghsa_3p69_m8gg_fwmf.rs`) that returns a 56-byte payload with the **new** custodian address at bytes `[36..56]`. [6](#0-5) 

4. Attacker calls `view` on the Aurora Engine targeting the malicious contract. `return_output` checks `&value[36..56] == CUSTODIAN_ADDRESS` — this is `false` because the new custodian address differs from the hardcoded one. The assert passes. [7](#0-6) 

5. The relayer receives the crafted `WithdrawResult`-shaped bytes and processes them as a legitimate withdrawal, releasing ETH from the Ethereum custodian to the attacker's address without any corresponding burn on Aurora.

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

**File:** engine-types/src/parameters/connector.rs (L9-18)
```rust
/// Eth-connector initial args
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq)]
pub struct InitCallArgs {
    pub prover_account: AccountId,
    pub eth_custodian_address: String,
    pub metadata: FungibleTokenMetadata,
}

/// Eth-connector Set contract data call args
pub type SetContractDataCallArgs = InitCallArgs;
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

**File:** engine-tests/src/tests/ghsa_3p69_m8gg_fwmf.rs (L21-31)
```rust
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
```
