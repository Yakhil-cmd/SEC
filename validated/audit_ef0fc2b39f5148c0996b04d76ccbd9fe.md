### Title
Forward/Proving Divergence: High-S Signature Bypass in `BasicBootloaderForwardSimulationConfig` — (File: `basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs`, `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

ZKsync OS correctly rejects malleable (high-S) signatures in the **proving** execution path. However, the **forward simulation** execution path (`BasicBootloaderForwardSimulationConfig`) unconditionally skips all signature validation — including the EIP-2 high-S check — due to `VALIDATE_EOA_SIGNATURE = false`. An attacker can craft a transaction with a high-S signature that is accepted during forward execution but rejected during proving, producing an unprovable block.

---

### Finding Description

Both validation implementations gate the entire signature check — including the `s > secp256k1n/2` rejection — behind a single boolean guard:

```rust
if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
    // No native for Eth STF  ← high-S check is SKIPPED here
} else {
    if U256::from_be_slice(s) > U256::from_be_bytes(SECP256K1N_HALF) {
        return Err(InvalidTransaction::MalleableSignature.into());
    }
    // ... ecrecover ...
}
``` [1](#0-0) [2](#0-1) 

The config definitions are:

```rust
pub struct BasicBootloaderForwardSimulationConfig;
impl BasicBootloaderExecutionConfig for BasicBootloaderForwardSimulationConfig {
    const VALIDATE_EOA_SIGNATURE: bool = false;  // ← skips ALL sig validation
    const SIMULATION: bool = false;
}

pub struct BasicBootloaderProvingExecutionConfig;
impl BasicBootloaderExecutionConfig for BasicBootloaderProvingExecutionConfig {
    const VALIDATE_EOA_SIGNATURE: bool = true;   // ← validates, rejects high-S
    const SIMULATION: bool = false;
}
``` [3](#0-2) [4](#0-3) 

`BasicBootloaderForwardSimulationConfig` is used by the forward system (`forward_system/src/run/mod.rs`) for the sequencer's pre-execution pass. Because `VALIDATE_EOA_SIGNATURE = false`, the condition `!false | false = true` causes the entire `else` branch — including the `SECP256K1N_HALF` check — to be bypassed. The proving path uses `BasicBootloaderProvingExecutionConfig` where `!true | false = false`, so the `else` branch runs and the high-S check is enforced. [5](#0-4) 

---

### Impact Explanation

A transaction with `s > secp256k1n/2` is accepted by the forward execution pass (sequencer includes it in the block) but rejected by the proving pass with `InvalidTransaction::MalleableSignature`. The resulting block is **unprovable**. In a ZK rollup, an unprovable block stalls finalization: the sequencer must detect the failure, reconstruct the block excluding the offending transaction, and re-submit — causing liveness degradation and potential L1 bridge delays. Repeated attacks can continuously stall block finalization. [6](#0-5) 

---

### Likelihood Explanation

Any unprivileged transaction sender can craft a valid ECDSA signature with `s > secp256k1n/2` (the malleable form of any existing signature). No special access, leaked keys, or governance majority is required. The attacker simply computes `s' = secp256k1n - s` and flips `v`, producing a signature that ecrecover accepts but that violates EIP-2. The forward run will accept it; the prover will not.

---

### Recommendation

Move the high-S check **outside** the `VALIDATE_EOA_SIGNATURE` gate so it is enforced in all execution modes (forward, proving, and ETH-like). The `ecrecover` call itself may be skipped for optimization, but the structural validity check on `s` is cheap and must be consistent across all configs:

```rust
// Always enforce EIP-2 high-S rejection, regardless of VALIDATE_EOA_SIGNATURE
if U256::from_be_slice(s) > U256::from_be_bytes(SECP256K1N_HALF) {
    return Err(InvalidTransaction::MalleableSignature.into());
}

if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
    // Skip ecrecover for optimization, but s-range is already validated above
} else {
    // ... ecrecover ...
}
```

Apply the same fix in both:
- `basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs`
- `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`

---

### Proof of Concept

1. Take any valid transaction signed with key `k`, producing `(r, s, v)` where `s ≤ secp256k1n/2`.
2. Compute the malleable form: `s' = secp256k1n - s`, `v' = 55 - v` (flip 27↔28).
3. Submit the transaction with `(r, s', v')` to the sequencer.
4. **Forward run** (`BasicBootloaderForwardSimulationConfig`): `VALIDATE_EOA_SIGNATURE = false` → condition `!false | false = true` → entire signature block skipped → transaction **accepted**, included in block.
5. **Proving run** (`BasicBootloaderProvingExecutionConfig`): `VALIDATE_EOA_SIGNATURE = true` → condition `!true | false = false` → enters `else` branch → `U256::from_be_slice(s') > SECP256K1N_HALF` is `true` → returns `Err(MalleableSignature)` → block **fails to prove**. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs (L201-206)
```rust
    if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
        // No native for Eth STF
    } else {
        if U256::from_be_slice(s) > U256::from_be_bytes(SECP256K1N_HALF) {
            return Err(InvalidTransaction::MalleableSignature.into());
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L253-262)
```rust
        if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
            intrinsic_resources.charge(&Resources::from_native(
                <<S as SystemTypes>::Resources as Resources>::Native::from_computational(
                    ECRECOVER_NATIVE_COST,
                ),
            ))?;
        } else {
            if U256::from_be_slice(s) > U256::from_be_bytes(SECP256K1N_HALF) {
                return Err(InvalidTransaction::MalleableSignature.into());
            }
```

**File:** basic_bootloader/src/bootloader/config.rs (L1-40)
```rust
pub trait BasicBootloaderExecutionConfig: 'static + Clone + Copy + core::fmt::Debug {
    /// Flag to disable EOA signature validation.
    /// It can be used to optimize forward run.
    const VALIDATE_EOA_SIGNATURE: bool;
    /// Simulation flag(used for `eth_call` and `estimate_gas`)
    const SIMULATION: bool;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderProvingExecutionConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderProvingExecutionConfig {
    const SIMULATION: bool = false;
    const VALIDATE_EOA_SIGNATURE: bool = true;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderForwardSimulationConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderForwardSimulationConfig {
    const VALIDATE_EOA_SIGNATURE: bool = false;
    const SIMULATION: bool = false;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderCallSimulationConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderCallSimulationConfig {
    // doesn't really matter, as `SIMULATION` disables signature validation anyway
    const VALIDATE_EOA_SIGNATURE: bool = true;
    const SIMULATION: bool = true;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderForwardETHLikeConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderForwardETHLikeConfig {
    const VALIDATE_EOA_SIGNATURE: bool = true;
    const SIMULATION: bool = false;
}
```

**File:** basic_bootloader/src/bootloader/errors.rs (L61-61)
```rust
    MalleableSignature,
```
