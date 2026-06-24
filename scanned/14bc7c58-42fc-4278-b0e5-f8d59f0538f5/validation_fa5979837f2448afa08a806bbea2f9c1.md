### Title
Missing `contextual_check_header_auxpow` Call in Legacy-Block Branch Bypasses `LegacyBlockNotAllowed` Enforcement — (`rs/bitcoin/validation/src/doge.rs`)

### Summary

`validate_auxpow_header` only calls `contextual_check_header_auxpow` (which enforces `LegacyBlockNotAllowed`) inside the `if let Some(aux_pow)` arm. The `else` arm — reached when `aux_pow=None` and `has_auxpow_bit()=false` — calls only `validate_header` (Scrypt PoW check) and never performs the AuxPoW-activation height guard. A header with no AuxPoW data and no AuxPoW bit set therefore passes validation at any post-activation height, as long as it carries valid Scrypt PoW at the computed target.

### Finding Description

In `validate_auxpow_header`:

```
if let Some(aux_pow) = header.aux_pow.as_ref() {
    // ...
    self.contextual_check_header_auxpow(&header.pure_header, height)?;  // ← only here
    // ...
} else {
    if header.has_auxpow_bit() {
        return Err(ValidateAuxPowHeaderError::InconsistentAuxPowBitSet);
    }
    self.validate_header(store, &header.pure_header)?;  // ← Scrypt PoW only
}
``` [1](#0-0) 

`contextual_check_header_auxpow` contains the sole enforcement of the post-activation legacy-block ban:

```rust
if !self.allow_legacy_blocks(height) && header.is_legacy() {
    return Err(ValidateAuxPowHeaderError::LegacyBlockNotAllowed);
}
``` [2](#0-1) 

Because this function is never invoked in the `else` branch, a `DogecoinHeader` with `aux_pow=None` and `has_auxpow_bit()=false` at any height above the AuxPoW activation threshold is validated purely by Scrypt PoW — the activation-height invariant is never checked.

The existing test suite actually demonstrates this: the test case labelled *"Version 2 (with correct chain ID) — should pass (after AuxPow activation)"* constructs exactly such a header against a post-activation store and asserts `is_ok()`, confirming the bypass is live in the current code. [3](#0-2) 

### Impact Explanation

`validate_auxpow_header` is the sole header-validation entry point for the Dogecoin adapter:

```rust
fn validate_header(
    &self,
    network: &bitcoin::dogecoin::Network,
    header: &DogecoinHeader,
) -> Result<(), Self::HeaderError> {
    let header_validator = DogecoinHeaderValidator::new(*network);
    header_validator.validate_auxpow_header(self, header)
}
``` [4](#0-3) 

An attacker who can feed headers to the ckDOGE adapter (via the normal peer/ingress path) and produce a valid Scrypt block at the current mainnet difficulty can insert a legacy block into the ckDOGE header chain at a post-AuxPoW height. A sufficiently long forged chain built on top of that block could be used to present fraudulent Dogecoin deposit transactions, leading to unauthorized ckDOGE minting.

### Likelihood Explanation

The barrier is real: the attacker must produce valid Scrypt PoW at the current mainnet difficulty, which requires substantial hash-rate. This is not trivially achievable by an ordinary unprivileged actor. However:

- The code path is unconditional — no configuration or privilege is required to reach it.
- A well-resourced attacker (e.g., a mining pool operator or someone who rents hash-rate) can produce the required PoW.
- The `contextual_check_header` function enforces the correct computed target, so the attacker cannot use `max_target`; they must match the real chain difficulty.

Likelihood is **low-to-medium** given the PoW cost, but the impact (fraudulent ckDOGE minting) is **high**.

### Recommendation

Call `contextual_check_header_auxpow` in the `else` branch as well, after computing the height via `contextual_check_header`:

```rust
} else {
    if header.has_auxpow_bit() {
        return Err(ValidateAuxPowHeaderError::InconsistentAuxPowBitSet);
    }
    let (target, height) = self.contextual_check_header(store, &header.pure_header)?;
    self.contextual_check_header_auxpow(&header.pure_header, height)?;
    // validate Scrypt PoW against the already-computed target
    if let Err(_) = header.pure_header.validate_pow_with_scrypt(target) {
        return Err(ValidateAuxPowHeaderError::ValidatePureHeader(
            ValidateHeaderError::InvalidPoWForComputedTarget,
        ));
    }
}
```

This mirrors the structure of the `if let Some(aux_pow)` arm and ensures the activation-height guard is applied symmetrically.

### Proof of Concept

The existing test at `rs/bitcoin/validation/src/tests/doge/auxpow.rs` lines 87–100 already demonstrates the bypass on regtest. For mainnet the analogous test would be:

1. Build a `DogecoinHeaderValidator::mainnet()` store at a height above the mainnet AuxPoW activation height.
2. Construct a `DogecoinHeader` with `aux_pow: None`, `has_auxpow_bit() = false`, and valid Scrypt PoW at the computed target.
3. Call `validate_auxpow_header`.
4. Observe `Ok(())` instead of `Err(LegacyBlockNotAllowed)`. [5](#0-4)

### Citations

**File:** rs/bitcoin/validation/src/doge.rs (L347-349)
```rust
        if !self.allow_legacy_blocks(height) && header.is_legacy() {
            return Err(ValidateAuxPowHeaderError::LegacyBlockNotAllowed);
        }
```

**File:** rs/bitcoin/validation/src/doge.rs (L366-409)
```rust
    fn validate_auxpow_header(
        &self,
        store: &impl HeaderStore,
        header: &DogecoinHeader,
    ) -> Result<(), ValidateAuxPowHeaderError> {
        if !header.is_legacy()
            && self.strict_chain_id()
            && header.extract_chain_id() != self.auxpow_chain_id()
        {
            return Err(ValidateAuxPowHeaderError::InvalidChainId);
        }

        if let Some(aux_pow) = header.aux_pow.as_ref() {
            if !header.has_auxpow_bit() {
                return Err(ValidateAuxPowHeaderError::InconsistentAuxPowBitSet);
            }

            let (target, height) = self.contextual_check_header(store, &header.pure_header)?;

            self.contextual_check_header_auxpow(&header.pure_header, height)?;

            if !target.is_met_by(aux_pow.parent_block_header.block_hash_with_scrypt()) {
                return Err(ValidateAuxPowHeaderError::InvalidParentPoW);
            }
            if aux_pow
                .check(
                    header.block_hash(),
                    header.extract_chain_id(),
                    self.strict_chain_id(),
                )
                .is_err()
            {
                return Err(ValidateAuxPowHeaderError::InvalidAuxPoW);
            }
        } else {
            if header.has_auxpow_bit() {
                return Err(ValidateAuxPowHeaderError::InconsistentAuxPowBitSet);
            }

            self.validate_header(store, &header.pure_header)?;
        }

        Ok(())
    }
```

**File:** rs/bitcoin/validation/src/tests/doge/auxpow.rs (L87-100)
```rust
    // Version 2 (with correct chain ID) - should pass (after AuxPow activation)
    let dogecoin_header = HeaderBuilder::default()
        .with_prev_header(prev_header_auxpow)
        .with_version(2)
        .with_chain_id(DOGECOIN_CHAIN_ID)
        .with_auxpow_bit(false)
        .with_valid_pow(true)
        .build()
        .into();
    assert!(
        validator
            .validate_auxpow_header(&store_auxpow, &dogecoin_header)
            .is_ok()
    );
```

**File:** rs/bitcoin/adapter/src/blockchainstate.rs (L86-93)
```rust
    fn validate_header(
        &self,
        network: &bitcoin::dogecoin::Network,
        header: &DogecoinHeader,
    ) -> Result<(), Self::HeaderError> {
        let header_validator = DogecoinHeaderValidator::new(*network);
        header_validator.validate_auxpow_header(self, header)
    }
```
