### Title
Unprotected Legacy Transactions Accepted Without Chain ID Binding, Enabling Cross-Chain Signature Replay - (File: `basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs`)

---

### Summary

ZKsync OS accepts pre-EIP-155 legacy transactions (v = 27 or 28) whose signing hash is computed without any chain ID. A signature produced on Ethereum mainnet (or any other EVM chain) for such a transaction is cryptographically identical on ZKsync OS, allowing an attacker to replay it and execute the transaction on ZKsync OS without the user's knowledge or consent.

---

### Finding Description

The transaction parsing pipeline in ZKsync OS handles legacy (type-0) transactions via `LegacyPayloadParser::try_parse_and_hash_for_signature_verification` in `legacy_tx.rs`. The function branches on whether the signature uses EIP-155 protection:

```rust
// legacy_tx.rs lines 89–114
let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
    // Unprotected legacy — NO chain ID in the hash
    let mut hasher = crypto::sha3::Keccak256::new();
    apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
    hasher.update(inner_slice);
    hasher.finalize_reset().into()
} else {
    // EIP-155 protected — chain ID IS included
    let min_v = U256::from(35) + U256::from(expected_chain_id) * U256::from(2);
    if !(legacy_signature.v == min_v || legacy_signature.v == min_v + U256::ONE) {
        return Err(InvalidTransaction::InvalidEncoding.into());
    }
    // ... chain_id mixed into hash ...
}
``` [1](#0-0) 

When `v == 27` or `v == 28`, `is_eip155()` returns `false` and the signing hash covers only the six payload fields (nonce, gasPrice, gasLimit, to, value, data) — no chain ID is mixed in. [2](#0-1) 

The parsed transaction is stored as the `RlpEncodedTxInner::Legacy` variant, and `chain_id()` explicitly returns `None` for it: [3](#0-2) 

The call site in `Transaction::try_from_buffer` does read the runtime chain ID from the system metadata and passes it as `expected_chain_id`: [4](#0-3) 

However, for the `Legacy` (non-EIP-155) branch, `expected_chain_id` is never used — it is only consumed by the EIP-155 branch. The unprotected path proceeds to signature verification against a chain-ID-free hash, so the recovered `from` address will match regardless of which chain the signature was originally created for. [5](#0-4) 

---

### Impact Explanation

Any unprotected legacy transaction (v = 27/28) that a user signs on Ethereum mainnet (chain_id = 1) or any other EVM chain produces a signature that is equally valid on ZKsync OS (chain_id = 324). An attacker who observes such a transaction on the source chain can submit the identical raw bytes to ZKsync OS. The bootloader will:

1. Compute the same chain-ID-free signing hash.
2. Recover the same `from` address via `secp256k1_ec_recover`.
3. Accept the transaction as originating from the victim.
4. Execute it — transferring value, calling contracts, or deploying code — on ZKsync OS without the user's consent.

The victim's funds on ZKsync OS are at risk even though they never intended to interact with ZKsync OS.

---

### Likelihood Explanation

Pre-EIP-155 transactions remain in use. Older hardware wallets, certain DeFi tooling, and some SDKs still produce v = 27/28 signatures. Ethereum's mempool is public, so any unprotected legacy transaction broadcast there is immediately observable by an attacker. The replay requires no privileged access: the attacker only needs to copy the raw transaction bytes and submit them to ZKsync OS. The nonce must match on ZKsync OS, but for a fresh ZKsync account (nonce = 0) this is trivially satisfied if the victim's Ethereum nonce was also 0 at the time of signing.

---

### Recommendation

**Short term:** Reject unprotected legacy transactions (v = 27/28) entirely at the parsing layer. ZKsync OS has its own chain ID and there is no compatibility requirement to accept pre-EIP-155 signatures. Add an explicit check:

```rust
if legacy_signature.is_eip155() == false {
    return Err(InvalidTransaction::InvalidChainId.into());
}
```

**Long term:** Audit all other signature-verification paths (e.g., ABI-encoded ZKsync-native transactions, EIP-7702 authorization entries with `chain_id = 0`) for analogous chain-ID omissions, and document the replay-protection guarantees of each transaction type.

---

### Proof of Concept

1. On Ethereum mainnet, Alice signs a legacy transfer of 1 ETH to Bob with nonce = 0, producing v = 27 (no EIP-155).
2. The raw RLP bytes are visible in the Ethereum mempool.
3. Alice also holds 1 ETH on ZKsync OS at the same address, with nonce = 0.
4. Attacker copies the raw bytes and submits them to ZKsync OS.
5. `LegacyPayloadParser::try_parse_and_hash_for_signature_verification` computes the same 6-field hash (no chain ID).
6. `secp256k1_ec_recover` recovers Alice's address — signature check passes.
7. The bootloader executes the transfer on ZKsync OS, sending Alice's ZKsync ETH to Bob without Alice's consent. [6](#0-5) [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs (L89-115)
```rust
        let sig_hash: Bytes32 = if legacy_signature.is_eip155() == false {
            // Unprotected legacy
            let mut hasher = crypto::sha3::Keccak256::new();
            apply_list_concatenation_encoding_to_hash(inner_slice.len() as u32, &mut hasher);
            hasher.update(inner_slice);
            hasher.finalize_reset().into()
        } else {
            // EIP-155 protected legacy: v must match 35 + 2*chainId (+ {0,1})
            let min_v = U256::from(35) + U256::from(expected_chain_id) * U256::from(2);
            if !(legacy_signature.v == min_v || legacy_signature.v == min_v + U256::ONE) {
                return Err(InvalidTransaction::InvalidEncoding.into());
            }

            // Compute signing hash over the 6-field payload plus chainId and two empty strings.
            let chain_id = expected_chain_id;
            let chain_id_encoding_len = u64_encoding_len(chain_id);

            let mut hasher = crypto::sha3::Keccak256::new();
            apply_list_concatenation_encoding_to_hash(
                (inner_slice.len() + chain_id_encoding_len + 2) as u32, // 0x80, 0x80 for r/s
                &mut hasher,
            );
            hasher.update(inner_slice);
            apply_u64_encoding_to_hash(chain_id, &mut hasher);
            hasher.update(&[0x80, 0x80]);
            hasher.finalize_reset().into()
        };
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/legacy_tx.rs (L128-131)
```rust
impl<'a> LegacySignatureData<'a> {
    pub fn is_eip155(&self) -> bool {
        self.v != 27 && self.v != 28
    }
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction.rs (L82-87)
```rust
    pub fn chain_id(&self) -> Option<u64> {
        match &self.inner {
            RlpEncodedTxInner::Legacy(_, _) => None,
            _ => Some(self.chain_id),
        }
    }
```

**File:** basic_bootloader/src/bootloader/transaction/mod.rs (L74-85)
```rust
        let expected_chain_id = system.get_chain_id();

        // query the transaction encoding format from the oracle
        let format: TxEncodingFormat = TxEncodingFormatQuery::get(system.io.oracle(), &())?;

        match format {
            TxEncodingFormat::Rlp => {
                // RLP-encoded transactions don't include the `from` field, so we need to query it from the oracle.
                // This is so that sequencer can skip ecrecover (for simulation, for example).
                let from = TxFromQuery::get(system.io.oracle(), &())?;
                let tx = RlpEncodedTransaction::parse_from_buffer(buffer, expected_chain_id, from)?;
                Ok(Self::Rlp(tx))
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/mod.rs (L119-134)
```rust
        } else {
            // Legacy path
            let (tx, sig_data, sig_hash) =
                LegacyPayloadParser::try_parse_and_hash_for_signature_verification(
                    input,
                    expected_chain_id,
                )?;

            let tx = if sig_data.is_eip155() {
                Self::LegacyWithEIP155(tx, sig_data)
            } else {
                Self::Legacy(tx, sig_data)
            };

            Ok((tx, sig_hash))
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs (L194-245)
```rust
    let suggested_signed_hash: Bytes32 = transaction.signed_hash()?;
    let from = *transaction.from();
    let Some((parity, r, s)) = transaction.sig_parity_r_s() else {
        // Ethereum txs should have signature
        return Err(InvalidTransaction::InvalidStructure.into());
    };

    if !Config::VALIDATE_EOA_SIGNATURE | Config::SIMULATION {
        // No native for Eth STF
    } else {
        if U256::from_be_slice(s) > U256::from_be_bytes(SECP256K1N_HALF) {
            return Err(InvalidTransaction::MalleableSignature.into());
        }

        let mut ecrecover_input = [0u8; 128];
        ecrecover_input[0..32].copy_from_slice(suggested_signed_hash.as_u8_array_ref());
        ecrecover_input[63] = (parity as u8) + 27;
        ecrecover_input[64..96][(32 - r.len())..].copy_from_slice(r);
        ecrecover_input[96..128][(32 - s.len())..].copy_from_slice(s);

        let mut ecrecover_output = ArrayBuilder::default();
        // We already charged gas for ecrecover in intrinsic cost, so we only need to charge native resources here.
        tx_resources
            .main_resources
            .with_infinite_ergs(|resources| {
                S::SystemFunctions::secp256k1_ec_recover(
                    ecrecover_input.as_slice(),
                    &mut ecrecover_output,
                    resources,
                    system.get_allocator(),
                )
                .map_err(SystemError::from)
            })?;

        if ecrecover_output.is_empty() {
            return Err(InvalidTransaction::IncorrectFrom {
                recovered: B160::ZERO,
                tx: from,
            }
            .into());
        }

        let recovered_from = B160::try_from_be_slice(&ecrecover_output.build()[12..])
            .ok_or(internal_error!("Invalid ecrecover return value"))?;

        if recovered_from != from {
            return Err(InvalidTransaction::IncorrectFrom {
                recovered: recovered_from,
                tx: from,
            }
            .into());
        }
```
