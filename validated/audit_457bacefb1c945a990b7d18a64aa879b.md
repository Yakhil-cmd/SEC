### Title
Unchecked Chained u128 Multiplication Overflow in `TokensToCycles::to_cycles()` Silently Produces Incorrect Cycles Amount - (File: `rs/nns/cmc/src/lib.rs`)

---

### Summary

The `TokensToCycles::to_cycles()` function in the Cycles Minting Canister (CMC) performs three sequential `u128` multiplications without any overflow guard. In Rust release builds (which IC canisters use), integer overflow wraps silently. If the intermediate product exceeds `u128::MAX`, the final cycles amount is silently wrong, causing either over-minting (ledger conservation break) or under-minting (user value loss) of cycles.

---

### Finding Description

The vulnerable code is:

```rust
// rs/nns/cmc/src/lib.rs, lines 359–366
impl TokensToCycles {
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128          // up to u64::MAX ≈ 1.84 × 10^19
                * self.xdr_permyriad_per_icp as u128  // up to u64::MAX ≈ 1.84 × 10^19
                * self.cycles_per_xdr.get()           // typically 1 × 10^12
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
    }
}
``` [1](#0-0) 

The three operands and their types:

| Operand | Type | Max value |
|---|---|---|
| `icpts.get_e8s()` | `u64` | `u64::MAX ≈ 1.84 × 10^19` |
| `xdr_permyriad_per_icp` | `u64` | `u64::MAX ≈ 1.84 × 10^19` |
| `cycles_per_xdr.get()` | `u128` | `u128::MAX`, typically `1 × 10^12` |

The first intermediate product `u64::MAX as u128 * u64::MAX as u128 = (2^64-1)^2 = 2^128 - 2^65 + 1` is just barely below `u128::MAX`. Multiplying that by `cycles_per_xdr` (even at its standard value of `1_000_000_000_000`) overflows `u128` completely.

`TOKEN_SUBDIVIDABLE_BY` is `100_000_000` (1e8). [2](#0-1) 

`DEFAULT_CYCLES_PER_XDR` is `1_000_000_000_000` (1T). [3](#0-2) 

The overflow threshold with standard `cycles_per_xdr = 1e12`:

```
icpts_e8s × xdr_permyriad_per_icp > u128::MAX / 1e12 ≈ 3.4 × 10^26
```

If ICP price reaches ~$1,000,000 USD (≈ 10^10 permyriad), overflow occurs when a user sends more than ~340 million ICP (≈ 3.4 × 10^16 e8s). The total ICP supply is approximately 500 million ICP, so this threshold is within the realm of a large holder at extreme prices.

The function is called from `tokens_to_cycles()` in `main.rs`, which is invoked on every `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` call: [4](#0-3) 

---

### Impact Explanation

In Rust release builds, `u128` arithmetic overflow wraps silently (two's complement). The wrapped result passed to `Cycles::new()` is an arbitrary value between 0 and `u128::MAX`. Two outcomes are possible:

1. **Over-minting (ledger conservation break):** The wrapped value is large → more cycles are minted than the ICP deposited is worth. The user receives a windfall of cycles at the protocol's expense, breaking the ICP↔cycles conservation invariant.
2. **Under-minting (user value loss):** The wrapped value is small → the user's ICP is burned but they receive far fewer cycles than owed.

Both outcomes are silent — no error is returned, no revert occurs. The ICP is burned in either case.

---

### Likelihood Explanation

**Low.** The overflow requires two conditions to coincide:
1. The NNS-governed `xdr_permyriad_per_icp` rate must be extremely high (ICP price in the hundreds of thousands of USD range).
2. A single user must send a very large ICP amount (hundreds of millions of ICP) in one transaction.

Neither condition is currently met, but neither is impossible. The rate is set by the exchange rate canister and NNS governance based on real market prices. The ICP amount is fully user-controlled within their balance. No privileged access is required on the user side.

---

### Recommendation

Replace the chained unchecked multiplication with a checked or saturating variant, or restructure to divide before multiplying to keep intermediate values small:

```rust
pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
    // Divide first to reduce intermediate magnitude, then multiply.
    // Or use a 256-bit intermediate (e.g., via u256 crate).
    let numerator: u128 = (icpts.get_e8s() as u128)
        .checked_mul(self.xdr_permyriad_per_icp as u128)
        .and_then(|v| v.checked_mul(self.cycles_per_xdr.get()))
        .expect("overflow in ICP→cycles conversion");
    Cycles::new(numerator / (TOKEN_SUBDIVIDABLE_BY as u128 * 10_000))
}
```

Or use a 256-bit intermediate (analogous to `FullMath.mulDiv`) to avoid phantom overflow while preserving precision.

---

### Proof of Concept

```
xdr_permyriad_per_icp = 10_000_000_000   // ICP at ~$1M (10^10 permyriad)
cycles_per_xdr        = 1_000_000_000_000 // standard 1T cycles/XDR
icpts_e8s             = 40_000_000_00_000_000  // 400M ICP (within total supply)

step1 = 4e15 * 1e10 = 4e25                // fits in u128
step2 = 4e25 * 1e12 = 4e37               // fits in u128 (< 3.4e38)
// At 500M ICP:
step1 = 5e16 * 1e10 = 5e26
step2 = 5e26 * 1e12 = 5e38 > u128::MAX  // OVERFLOW → wraps to wrong value
result = wrapped_value / (1e8 * 1e4)     // silently incorrect cycles amount
```

The user's ICP is burned by the ledger, but `Cycles::new(wrapped_value)` mints an incorrect number of cycles — either a massive windfall or near-zero — with no error surfaced to the caller. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/lib.rs (L22-22)
```rust
pub const DEFAULT_CYCLES_PER_XDR: u128 = 1_000_000_000_000_u128; // 1T cycles = 1 XDR
```

**File:** rs/nns/cmc/src/lib.rs (L358-367)
```rust
impl TokensToCycles {
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128
                * self.xdr_permyriad_per_icp as u128
                * self.cycles_per_xdr.get()
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
    }
}
```

**File:** rs/ledger_suite/common/ledger_core/src/tokens.rs (L137-137)
```rust
pub const TOKEN_SUBDIVIDABLE_BY: u64 = 100_000_000;
```

**File:** rs/nns/cmc/src/main.rs (L1900-1923)
```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            None => {
                let error_message =
                    "No conversion rate found in CMC, notification aborted".to_string();
                print(&error_message);
                Err(NotifyError::Other {
                    error_code: NotifyErrorCode::Internal as u64,
                    error_message,
                })
            }
        }
    })
}
```

**File:** rs/nns/cmc/src/main.rs (L1965-1966)
```rust
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
```
