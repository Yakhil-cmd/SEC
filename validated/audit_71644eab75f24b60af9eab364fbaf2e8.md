### Title
Zero Native Resource Charge in All EIP-2537 BLS12-381 Precompiles Enables Prover DoS — (File: `basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/pairing.rs`, `msm.rs`, `addition.rs`, `mappings.rs`)

---

### Summary

All seven EIP-2537 BLS12-381 precompiles (`G1Add`, `G2Add`, `G1MSM`, `G2MSM`, `PairingCheck`, `MapFpToG1`, `MapFp2ToG2`) hardcode `cost_native = 0`, charging **zero native (proving) resource** for extremely expensive elliptic curve operations. An unprivileged transaction sender can craft transactions that force the prover to perform unbounded BLS12-381 work without consuming any native resource budget, enabling a systematic DoS against the ZK proving system.

---

### Finding Description

ZKsync OS implements a **double resource accounting** model: every operation charges both EVM gas (ergs) and a **native resource** that models the actual ZK proving cost. All other computationally intensive precompiles charge appropriate native resources:

- `ecrecover`: `ECRECOVER_NATIVE_COST = native_with_delegations!(350_000, 43_000, 0)`
- `sha256`: `SHA256_BASE_NATIVE_COST + nb_rounds * SHA256_ROUND_NATIVE_COST`
- `ripemd160`: `RIPEMD160_BASE_NATIVE_COST + nb_rounds * RIPEMD160_ROUND_NATIVE_COST`
- `modexp`: `ergs / ERGS_PER_GAS * MODEXP_WORST_CASE_NATIVE_PER_GAS`
- BN254 pairing: `BN254_PAIRING_BASE_NATIVE_COST = native_with_delegations!(13_000_000, 500_000, 0)`

However, every single EIP-2537 BLS12-381 precompile sets `cost_native = 0`:

**`pairing.rs` line 36:**
```rust
let cost_native = 0;
resources.charge(&R::from_ergs_and_native(
    cost_ergs,
    <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
))?;
```

**`msm.rs` lines 198 and 266** (G1MSM and G2MSM):
```rust
let cost_native = 0;
resources.charge(&R::from_ergs_and_native(
    cost_ergs,
    <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
))?;
```

**`addition.rs` lines 22 and 63** (G1Add and G2Add):
```rust
let cost_native = 0;
```

**`mappings.rs` lines 26 and 76** (MapFpToG1 and MapFp2ToG2):
```rust
let cost_native = 0;
```

BLS12-381 pairing is cryptographically far more expensive than BN254 pairing in ZK proving circuits. BN254 pairing charges `13,000,000` native units per call; BLS12-381 pairing charges `0`. The `multi_pairing` call in `pairing.rs` line 68 invokes a full Miller loop and final exponentiation over all input pairs — a massive proving workload — with no native resource deduction.

The `G1MSM` and `G2MSM` precompiles are similarly affected: they invoke the Pippenger multi-scalar multiplication algorithm (`msm()` at lines 232 and 301) over an unbounded number of input pairs, all at zero native cost.

---

### Impact Explanation

The native resource budget is the mechanism that prevents a block from containing more proving work than the prover can handle. When `cost_native = 0`, the native resource limit is never reached for BLS12-381 operations. An attacker can:

1. Fill a block with transactions calling `BLS12_PAIRING_CHECK` (address `0x0f`) with the maximum number of pairs permitted by the EVM gas limit.
2. Each transaction pays EVM gas (`37700 + 32600 * N` gas) but consumes **zero native resource**.
3. The block's native resource budget is unaffected, so additional BLS12-381 transactions can be packed into the same block.
4. The prover must execute all these multi-pairings at enormous computational cost, far exceeding what the native resource budget would normally allow.

This is a **resource accounting bug** that breaks the invariant that native resource consumption bounds prover work per block. The impact is a DoS against the ZK prover: the prover is forced to process work that was never accounted for in the block's resource budget.

---

### Likelihood Explanation

- The `eip-2537` feature enables these precompiles in production builds.
- Any unprivileged EOA can send a transaction calling address `0x0f` (or `0x0c`, `0x0e`, etc.) with valid BLS12-381 encoded inputs.
- No special privileges, governance access, or oracle manipulation is required.
- The attack is cheap: the attacker pays only EVM gas, which is the normal cost of calling these precompiles.
- The attack is repeatable across every block.

---

### Recommendation

Add proper native resource costs to all EIP-2537 BLS12-381 precompiles, analogous to the BN254 precompiles. Add constants to `basic_system/src/cost_constants.rs` (e.g., `BLS12_381_PAIRING_BASE_NATIVE_COST`, `BLS12_381_PAIRING_PER_PAIR_NATIVE_COST`, `BLS12_381_G1MSM_PER_POINT_NATIVE_COST`, etc.) benchmarked against actual RISC-V proving cycles, and replace `let cost_native = 0;` in all seven EIP-2537 precompile implementations.

---

### Proof of Concept

An attacker sends a transaction to address `0x000000000000000000000000000000000000000f` (BLS12_PAIRING_CHECK) with `N` valid G1/G2 pairs encoded per EIP-2537. With a 30M gas block limit, approximately `(30_000_000 - 37_700) / 32_600 ≈ 919` pairs can be submitted per transaction. Each pair triggers a full BLS12-381 Miller loop iteration in `multi_miller_loop` (`crypto/src/bls12_381/curves/pairing_impl.rs` line 98) and a final exponentiation (`final_exponentiation` line 144). The native resource charged is `0` in all cases (`pairing.rs` line 36), so the block's native budget is not consumed and additional such transactions can be packed. The prover must execute all multi-pairings with no native resource accounting, violating the block's proving cost bound. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/pairing.rs (L32-40)
```rust
        let cost_ergs = Ergs(
            ((num_pairs as u64) * BLS12_381_PAIRING_PER_PAIR_GAS + BLS12_381_PAIRING_FIXED_GAS)
                * ERGS_PER_GAS,
        );
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/pairing.rs (L68-68)
```rust
        let pairing_result = <Bls12_381 as Pairing>::multi_pairing(g1_points, g2_points);
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L196-202)
```rust
        );
        let cost_ergs = Ergs(cost * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L264-270)
```rust
        );
        let cost_ergs = Ergs(cost * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/addition.rs (L21-26)
```rust
        let cost_ergs = Ergs(BLS12_381_G1_ADDITION_GAS * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mappings.rs (L25-30)
```rust
        let cost_ergs = Ergs(BLS12_381_FIELD_TO_G1_GAS * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_system/src/cost_constants.rs (L45-50)
```rust
pub const BN254_ECADD_NATIVE_COST: u64 = native_with_delegations!(46_000, 1650, 0);
pub const BN254_ECMUL_NATIVE_COST: u64 = native_with_delegations!(600_000, 41_000, 0);
pub const BN254_PAIRING_BASE_NATIVE_COST: u64 = native_with_delegations!(13_000_000, 500_000, 0);
pub const BN254_PAIRING_PER_PAIR_NATIVE_COST: u64 = BN254_PAIRING_BASE_NATIVE_COST;
pub const MODEXP_WORST_CASE_NATIVE_PER_GAS: u64 = 300;
pub const P256_NATIVE_COST: u64 = native_with_delegations!(500_000, 71_000, 0);
```

**File:** crypto/src/bls12_381/curves/pairing_impl.rs (L98-142)
```rust
    fn multi_miller_loop(
        a: impl IntoIterator<Item = impl Into<Self::G1Prepared>>,
        b: impl IntoIterator<Item = impl Into<Self::G2Prepared>>,
    ) -> ark_ec::pairing::MillerLoopOutput<Self> {
        let mut a = a.into_iter();
        let mut b = b.into_iter();
        let mut result = Fq12::one();
        loop {
            match (a.next(), b.next()) {
                (Some(p), Some(q)) => {
                    let p: Self::G1Prepared = p.into();
                    if p.is_zero() {
                        continue;
                    }
                    let q: Self::G2Prepared = q.into();
                    if q.is_zero() {
                        continue;
                    }

                    let mut f = Fq12::one();
                    let mut ell_coeffs = q.ell_coeffs.iter();

                    for i in BitIteratorBE::without_leading_zeros(Config::X).skip(1) {
                        f.square_in_place();
                        Self::ell(&mut f, &ell_coeffs.next().unwrap(), &p.0);
                        if i {
                            Self::ell(&mut f, &ell_coeffs.next().unwrap(), &p.0);
                        }
                    }

                    result *= f;
                }
                (None, None) => break,
                _ => {
                    panic!("Caller must check input lengths");
                }
            }
        }

        if Config::X_IS_NEGATIVE {
            result.cyclotomic_inverse_in_place();
        }

        ark_ec::pairing::MillerLoopOutput(result)
    }
```
