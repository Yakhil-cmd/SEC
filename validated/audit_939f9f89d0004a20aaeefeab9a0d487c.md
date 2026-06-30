### Title
Pre-EIP-155 Legacy Transactions Accepted Without Chain ID Validation Enables Cross-Chain Replay Attack - (File: `engine-transactions/src/legacy.rs`, `engine/src/engine.rs`)

### Summary

Aurora Engine explicitly accepts pre-EIP-155 legacy transactions (`v = 27` or `v = 28`) whose signed payload contains no chain ID. The chain ID validation in `submit_with_alt_modexp` is gated on the transaction carrying a chain ID, so pre-EIP-155 transactions bypass it entirely. Any pre-EIP-155 transaction signed on Ethereum mainnet (or any other EVM chain) can be submitted verbatim to Aurora Engine and will execute if the sender's Aurora nonce matches, enabling direct theft of the sender's Aurora ETH.

### Finding Description

**Root cause — signature construction (`engine-transactions/src/legacy.rs`):**

`LegacyEthSignedTransaction::sender()` decodes `v = 27..=28` as pre-EIP-155, setting `chain_id = None` and hashing only the six-field RLP `(nonce, gas_price, gas_limit, to, value, data)` — no chain ID is committed to in the signed message. [1](#0-0) 

**Root cause — chain ID enforcement (`engine/src/engine.rs`):**

The only chain ID check in the submission pipeline is:

```rust
if let Some(chain_id) = transaction.chain_id
    && U256::from(chain_id) != U256::from_big_endian(&state.chain_id)
{
    return Err(EngineErrorKind::InvalidChainId.into());
}
```

When `chain_id` is `None` (pre-EIP-155), the entire guard is skipped. Aurora Engine then proceeds to execute the transaction normally. [2](#0-1) 

This behaviour was deliberately re-enabled to support EIP-1820 deterministic deployment: [3](#0-2) 

The intentional nature of the decision does not eliminate the security consequence: the signed payload commits to no domain identifier, making it valid on every EVM chain that accepts pre-EIP-155 transactions.

**Attack path:**

1. Victim signs a pre-EIP-155 ETH transfer on Ethereum mainnet at nonce N (e.g., `to = attacker, value = X`).
2. Victim also holds ETH on Aurora at nonce N (common for accounts that started on Aurora before accumulating Ethereum history, or for fresh accounts on both chains).
3. Attacker submits the identical signed bytes to Aurora's `submit` entry point.
4. Aurora recovers the sender correctly (same ECDSA math, no chain ID in the hash), passes the nonce check, and executes the transfer — draining the victim's Aurora ETH to the attacker.

### Impact Explanation

**Direct theft of user funds (Critical).** The attacker needs no privileged access. The victim's ETH on Aurora is transferred to an attacker-controlled address without the victim's consent for Aurora. The victim signed only for Ethereum; the signature is equally valid on Aurora because the signed preimage is chain-agnostic.

### Likelihood Explanation

**Moderate.** Pre-EIP-155 transactions are rare in current wallets but are common in historical on-chain data (pre-2016 Ethereum, EIP-1820 deployments, some hardware-wallet flows). The nonce must coincide on both chains, which is realistic for:
- New Aurora users whose first Aurora nonce (0) matches a historical Ethereum nonce-0 transaction.
- Users who replicated the same sequence of transactions on both chains.
- Automated scripts or bots that sign without EIP-155.

An attacker can monitor the Ethereum mempool or historical chain data for pre-EIP-155 transactions, then immediately replay them on Aurora.

### Recommendation

Reject pre-EIP-155 transactions at the `submit` boundary unless a specific allow-list of known safe use-cases (e.g., the EIP-1820 deployer address) is enforced:

```rust
// In submit_with_alt_modexp, after recovering the sender:
if transaction.chain_id.is_none() && !is_eip1820_deployer(&sender) {
    return Err(EngineErrorKind::InvalidChainId.into());
}
```

Alternatively, document the accepted risk explicitly in the security model and warn users that pre-EIP-155 signatures are chain-agnostic on Aurora.

### Proof of Concept

```
1. On Ethereum mainnet, broadcast (or find in history):
   LegacyTx { nonce: 0, to: <attacker>, value: 1 ETH, v: 27, r: R, s: S }
   (signed without chain ID — v=27 means pre-EIP-155)

2. Victim has 1 ETH on Aurora at nonce 0.

3. Attacker calls aurora.submit(rlp_encode(tx)) on NEAR.

4. engine/src/engine.rs:
   - transaction.chain_id == None  →  chain ID check skipped
   - check_nonce passes (nonce 0 matches)
   - EVM executes: transfer 1 ETH to attacker

5. Victim's Aurora ETH is gone; attacker received it.
``` [4](#0-3) [5](#0-4)

### Citations

**File:** engine-transactions/src/legacy.rs (L63-84)
```rust
impl LegacyEthSignedTransaction {
    /// Returns sender of given signed transaction by doing ecrecover on the signature.
    pub fn sender(&self) -> Result<Address, Error> {
        let mut rlp_stream = RlpStream::new();
        // See details of CHAIN_ID computation here - https://github.com/ethereum/EIPs/blob/master/EIPS/eip-155.md#specification
        let (chain_id, rec_id) = match self.v {
            0..=26 | 29..=34 => return Err(Error::InvalidV),
            27..=28 => (
                None,
                u8::try_from(self.v - 27).map_err(|_e| Error::InvalidV)?,
            ),
            _ => (
                Some((self.v - 35) / 2),
                u8::try_from((self.v - 35) % 2).map_err(|_e| Error::InvalidV)?,
            ),
        };
        self.transaction
            .rlp_append_unsigned(&mut rlp_stream, chain_id);
        let message_hash = sdk::keccak(rlp_stream.as_raw());
        sdk::ecrecover(message_hash, &super::vrs_to_arr(rec_id, self.r, self.s))
            .map_err(|_| Error::EcRecover)
    }
```

**File:** engine/src/engine.rs (L1054-1063)
```rust
    // Validate the chain ID, if provided inside the signature:
    if let Some(chain_id) = transaction.chain_id
        && U256::from(chain_id) != U256::from_big_endian(&state.chain_id)
    {
        return Err(EngineErrorKind::InvalidChainId.into());
    }

    sdk::log!("signer_address {:?}", sender);

    check_nonce(&io, &sender, &transaction.nonce)?;
```

**File:** CHANGES.md (L586-586)
```markdown
- Original ETH transactions which do not contain a Chain ID are allowed again to allow for use of [EIP-1820] by [@joshuajbouw]. ([#520])
```
