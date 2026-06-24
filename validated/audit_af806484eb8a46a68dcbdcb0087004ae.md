The code is confirmed exactly as described. Let me verify the key structural details before rendering the verdict.

Audit Report

## Title
Missing `contextual_check_header_auxpow` call in legacy (non-AuxPow) path of `validate_auxpow_header` allows post-fork legacy blocks and version-obsolete headers to bypass enforcement — (`rs/bitcoin/validation/src/doge.rs`)

## Summary
`validate_auxpow_header` branches on `header.aux_pow`. The AuxPow branch correctly calls `contextual_check_header_auxpow`, which enforces both the `LegacyBlockNotAllowed` rule (post-AuxPow-fork legacy blocks must be rejected) and the `VersionObsolete` rule (version < 3 at `height >= bip66_height`, version < 4 at `height >= bip65_height`). The legacy branch (`aux_pow == None`) calls only `validate_header`, which performs PoW/difficulty/timestamp/checkpoint checks but contains no version or fork-era checks. A crafted `DogecoinHeader` with `aux_pow: None`, `has_auxpow_bit() == false`, and valid Scrypt PoW at post-fork height passes the legacy branch without triggering either guard, causing the ckDOGE adapter to accept a block the canonical Dogecoin network would reject.

## Finding Description
In `rs/bitcoin/validation/src/doge.rs`, `contextual_check_header_auxpow` (lines 342–362) is the sole location of two enforcement rules:

1. **`LegacyBlockNotAllowed`** (lines 347–349): rejects legacy blocks at heights where `allow_legacy_blocks(height)` is false (i.e., post-AuxPow-fork, height ≥ ~371,337 on mainnet).
2. **`VersionObsolete`** (lines 355–359): rejects `version < 3` at `height >= bip66_height` and `version < 4` at `height >= bip65_height`.

`validate_auxpow_header` (lines 366–409) has two branches:

- **AuxPow path** (`aux_pow == Some(...)`), lines 378–399: calls `contextual_check_header_auxpow` at line 385 — both guards enforced.
- **Legacy path** (`aux_pow == None`), lines 400–406: checks only `has_auxpow_bit()` consistency (line 401–403) then calls `self.validate_header(store, &header.pure_header)` (line 405) — neither guard is reachable.

`validate_header` (lines 157–174) calls `contextual_check_header` (line 162) for difficulty/timestamp/checkpoint, then validates Scrypt PoW (line 164). It has no awareness of the AuxPow fork era or version obsolescence.

Exploit flow:
1. Attacker constructs a `DogecoinHeader` with `aux_pow: None`, `has_auxpow_bit() == false`, `version == 1`, `prev_blockhash` pointing to a block already in the adapter's store, and the correct difficulty target for that height.
2. Attacker mines a valid Scrypt PoW satisfying that target.
3. `validate_auxpow_header` takes the `else` branch; `has_auxpow_bit()` is false so no early return; `validate_header` passes (PoW/difficulty/timestamp/checkpoint all valid).
4. Neither `LegacyBlockNotAllowed` nor `VersionObsolete` is checked. The header is accepted.

## Impact Explanation
The ckDOGE adapter is the Chain Fusion component that tracks the Dogecoin chain for ckDOGE minting and burning. Accepting a post-fork legacy block causes the adapter's internal chain to diverge from the canonical Dogecoin chain. The adapter will track a fork that does not exist on the real Dogecoin network, leading to incorrect UTXO set state. Downstream effects include incorrect ckDOGE deposit crediting and withdrawal processing, with potential for double-spend or permanent loss of user funds in the chain-fusion bridge. This matches the allowed impact: **Significant Chain Fusion / ck-token security impact with concrete user or protocol harm** (High, $2,000–$10,000).

## Likelihood Explanation
Exploitation requires the attacker to produce a Scrypt PoW solution at the current Dogecoin mainnet difficulty, which is substantial but achievable by a mining pool or well-resourced adversary. No privileged access to the IC protocol is required — the attacker only needs to submit a crafted header to the adapter's P2P layer. The `LegacyBlockNotAllowed` bypass is the more impactful vector; the `VersionObsolete` bypass is a secondary consequence of the same missing call. The bug is deterministically reproducible in a unit test without any real PoW using a mock store.

## Recommendation
In the `else` branch of `validate_auxpow_header`, after the `has_auxpow_bit()` consistency check, call `contextual_check_header_auxpow` with the height derived from `contextual_check_header`. Since `validate_header` already calls `contextual_check_header` internally (line 162) and discards the height, the simplest fix is to call `contextual_check_header` explicitly in the `else` branch before `validate_header`:

```rust
} else {
    if header.has_auxpow_bit() {
        return Err(ValidateAuxPowHeaderError::InconsistentAuxPowBitSet);
    }
    let (_, height) = self.contextual_check_header(store, &header.pure_header)?;
    self.contextual_check_header_auxpow(&header.pure_header, height)?;
    self.validate_header(store, &header.pure_header)?;
}
```

Alternatively, refactor `validate_header` to accept a pre-computed `(Target, BlockHeight)` to avoid the redundant `contextual_check_header` call.

## Proof of Concept
```rust
// Unit test — no real PoW needed with a mock store
let validator = DogecoinHeaderValidator::mainnet();
// Store tip at height = bip66_height - 1, so next block is at bip66_height
let store = MockHeaderStore::with_prev_at_height(bip66_height - 1);
let header = DogecoinHeader {
    pure_header: PureHeader {
        version: 1,           // obsolete version
        prev_blockhash: store.tip_hash(),
        // ... valid bits/time/nonce for the mock store's difficulty
    },
    aux_pow: None,            // takes the legacy branch
};
// Currently returns Ok(()); should return Err(VersionObsolete)
assert_eq!(
    validator.validate_auxpow_header(&store, &header),
    Err(ValidateAuxPowHeaderError::VersionObsolete)
);

// Second variant: post-fork height, legacy block should be rejected
let store2 = MockHeaderStore::with_prev_at_height(auxpow_fork_height - 1);
assert_eq!(
    validator.validate_auxpow_header(&store2, &header),
    Err(ValidateAuxPowHeaderError::LegacyBlockNotAllowed)
);
```