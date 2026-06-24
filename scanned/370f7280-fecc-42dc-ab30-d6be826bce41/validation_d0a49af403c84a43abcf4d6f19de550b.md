### Title
Hardcoded Ethereum `chain_id` in `EthereumNetwork` Enum Prevents Replay Protection After Ethereum Hard Fork - (File: `rs/ethereum/cketh/minter/src/lifecycle.rs`)

---

### Summary

The ckETH minter canister on the Internet Computer hardcodes the Ethereum `chain_id` as a compile-time constant inside the `EthereumNetwork` enum. This value is set once at canister initialization and is permanently baked into every EIP-1559 withdrawal transaction signed by the minter's threshold ECDSA key. If Ethereum undergoes a hard fork that changes the chain ID (as happened with the ETH/ETC split), the minter will continue signing transactions with the pre-fork chain ID, enabling cross-chain replay attacks where signed withdrawal transactions valid on the original chain are also valid on the forked chain, causing double-spend of ckETH/ckERC20 funds.

---

### Finding Description

The `EthereumNetwork` enum in `rs/ethereum/cketh/minter/src/lifecycle.rs` maps each network variant to a fixed `chain_id` via a `match` expression:

```rust
impl EthereumNetwork {
    pub fn chain_id(&self) -> u64 {
        match self {
            EthereumNetwork::Mainnet => 1,
            EthereumNetwork::Sepolia => 11155111,
        }
    }
}
``` [1](#0-0) 

This `chain_id` is consumed directly in `create_transaction()` when constructing every `Eip1559TransactionRequest`:

```rust
Ok(Eip1559TransactionRequest {
    chain_id: ethereum_network.chain_id(),
    ...
})
``` [2](#0-1) [3](#0-2) 

The `chain_id` field is then RLP-encoded into the transaction hash that is signed by the threshold ECDSA key:

```rust
pub fn rlp_inner(&self, rlp: &mut RlpStream) {
    rlp.append(&self.chain_id);
    ...
}
pub fn hash(&self) -> Hash {
    let mut bytes = self.rlp_bytes().to_vec();
    bytes.insert(0, self.transaction_type());
    Hash(ic_sha3::Keccak256::hash(bytes))
}
``` [4](#0-3) 

The `UpgradeArg` struct — the only mechanism to update minter state post-deployment — contains no field for `ethereum_network` or `chain_id`:

```rust
pub struct UpgradeArg {
    pub next_transaction_nonce: Option<Nat>,
    pub minimum_withdrawal_amount: Option<Nat>,
    pub ethereum_contract_address: Option<String>,
    pub ethereum_block_height: Option<CandidBlockTag>,
    pub ledger_suite_orchestrator_id: Option<Principal>,
    pub erc20_helper_contract_address: Option<String>,
    ...
    // NO ethereum_network / chain_id field
}
``` [5](#0-4) 

The `State::upgrade()` function correspondingly has no branch to update `ethereum_network`. [6](#0-5) 

The `EthereumNetwork` enum is also restricted to exactly two variants with no `TryFrom` path for any other chain ID:

```rust
impl TryFrom<u64> for EthereumNetwork {
    fn try_from(value: u64) -> Result<Self, Self::Error> {
        match value {
            1 => Ok(EthereumNetwork::Mainnet),
            11155111 => Ok(EthereumNetwork::Sepolia),
            _ => Err("Unknown Ethereum Network".to_string()),
        }
    }
}
``` [7](#0-6) 

---

### Impact Explanation

If Ethereum undergoes a contentious hard fork producing two chains — one retaining chain ID `1` and one adopting a new chain ID — the ckETH minter will continue to sign all withdrawal transactions with `chain_id = 1`. A signed EIP-1559 transaction is valid on any chain that accepts that chain ID. An attacker who observes a legitimate ckETH withdrawal transaction broadcast on the original chain can replay the identical signed transaction on the forked chain (which still accepts chain ID `1`), causing the minter's Ethereum address to send ETH/ERC-20 tokens on the forked chain without any corresponding ckETH burn on the IC. This results in:

- **Double-spend**: the minter's ETH balance on the forked chain is drained without any IC-side accounting.
- **Permanent loss of funds**: the minter holds real ETH/ERC-20 tokens; replayed transactions transfer them to the attacker's address on the fork.
- **No recovery path**: `UpgradeArg` cannot update `ethereum_network`, so the minter cannot be reconfigured to use the new chain ID without a full canister reinstall (which would lose all state).

---

### Likelihood Explanation

Ethereum hard forks are a documented, real-world event (ETH/ETC split in 2016 is the canonical example). EIP-155 was specifically introduced to prevent cross-chain replay attacks by including `chain_id` in transaction signing. The ckETH minter correctly implements EIP-155 for normal operation, but the chain ID is immutable after deployment. Any future contentious Ethereum fork that preserves chain ID `1` on both branches would immediately expose all pending and future withdrawal transactions to replay. The attacker entry path requires only the ability to observe Ethereum mempool transactions (fully public) and submit them to the forked chain — no privileged access is needed.

---

### Recommendation

1. Add an `ethereum_network: Option<EthereumNetwork>` field to `UpgradeArg` so that the NNS governance can update the chain ID via a canister upgrade in response to a hard fork.
2. Alternatively, extend `EthereumNetwork` to carry an arbitrary `chain_id: u64` rather than hardcoding it, and expose a governance-controlled update path.
3. At minimum, document the hard-fork risk and establish an incident-response procedure that includes a canister reinstall path with state migration.

---

### Proof of Concept

1. The ckETH minter is initialized with `ethereum_network = EthereumNetwork::Mainnet`, fixing `chain_id = 1` permanently. [8](#0-7) 

2. Every withdrawal creates an `Eip1559TransactionRequest` with `chain_id: ethereum_network.chain_id()` (= `1`). [9](#0-8) 

3. The transaction is signed via threshold ECDSA over the RLP hash that includes `chain_id`. [10](#0-9) 

4. Suppose Ethereum forks: chain A keeps chain ID `1`, chain B adopts chain ID `2`. The signed transaction `0x02f873...` (chain ID `1`) is valid on chain A. An attacker submits the same bytes to chain B's mempool. Chain B nodes that still accept chain ID `1` transactions (pre-fork nodes or replay-unprotected nodes) will execute the transfer, draining the minter's ETH on chain B without any ckETH burn on the IC.

5. The `UpgradeArg` has no `ethereum_network` field, so the minter cannot be reconfigured to chain ID `2` without a full reinstall. [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/lifecycle.rs (L34-41)
```rust
impl EthereumNetwork {
    pub fn chain_id(&self) -> u64 {
        match self {
            EthereumNetwork::Mainnet => 1,
            EthereumNetwork::Sepolia => 11155111,
        }
    }
}
```

**File:** rs/ethereum/cketh/minter/src/lifecycle.rs (L43-52)
```rust
impl TryFrom<u64> for EthereumNetwork {
    type Error = String;

    fn try_from(value: u64) -> Result<Self, Self::Error> {
        match value {
            1 => Ok(EthereumNetwork::Mainnet),
            11155111 => Ok(EthereumNetwork::Sepolia),
            _ => Err("Unknown Ethereum Network".to_string()),
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1135-1136)
```rust
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1169-1170)
```rust
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L431-451)
```rust
    pub fn rlp_inner(&self, rlp: &mut RlpStream) {
        rlp.append(&self.chain_id);
        rlp.append(&self.nonce);
        rlp.append(&self.max_priority_fee_per_gas);
        rlp.append(&self.max_fee_per_gas);
        rlp.append(&self.gas_limit);
        rlp.append(&self.destination.as_ref());
        rlp.append(&self.amount);
        rlp.append(&self.data);
        rlp.append(&self.access_list);
    }

    /// Hash of EIP-1559 transaction is computed as
    /// keccak256(0x02 || rlp([chain_id, nonce, max_priority_fee_per_gas, max_fee_per_gas, gas_limit, destination, amount, data, access_list])),
    /// where `||` denotes string concatenation.
    pub fn hash(&self) -> Hash {
        use rlp::Encodable;
        let mut bytes = self.rlp_bytes().to_vec();
        bytes.insert(0, self.transaction_type());
        Hash(ic_sha3::Keccak256::hash(bytes))
    }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L461-484)
```rust
    pub async fn sign(self) -> Result<SignedEip1559TransactionRequest, String> {
        let hash = self.hash();
        let key_name = read_state(|s| s.ecdsa_key_name.clone());
        let signature = crate::management::sign_with_ecdsa(
            key_name,
            DerivationPath::new(crate::MAIN_DERIVATION_PATH),
            hash.0,
        )
        .await
        .map_err(|e| format!("failed to sign tx: {e}"))?;
        let recid = compute_recovery_id(&hash, &signature).await;
        if recid.is_x_reduced() {
            return Err("BUG: affine x-coordinate of r is reduced which is so unlikely to happen that it's probably a bug".to_string());
        }
        let (r_bytes, s_bytes) = split_in_two(signature);
        let r = u256::from_be_bytes(r_bytes);
        let s = u256::from_be_bytes(s_bytes);
        let sig = Eip1559Signature {
            signature_y_parity: recid.is_y_odd(),
            r,
            s,
        };

        Ok(SignedEip1559TransactionRequest::new(self, sig))
```

**File:** rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs (L11-33)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default, CandidType, Decode, Deserialize, Encode)]
pub struct UpgradeArg {
    #[cbor(n(0), with = "icrc_cbor::nat::option")]
    pub next_transaction_nonce: Option<Nat>,
    #[cbor(n(1), with = "icrc_cbor::nat::option")]
    pub minimum_withdrawal_amount: Option<Nat>,
    #[n(2)]
    pub ethereum_contract_address: Option<String>,
    #[n(3)]
    pub ethereum_block_height: Option<CandidBlockTag>,
    #[cbor(n(4), with = "icrc_cbor::principal::option")]
    pub ledger_suite_orchestrator_id: Option<Principal>,
    #[n(5)]
    pub erc20_helper_contract_address: Option<String>,
    #[cbor(n(6), with = "icrc_cbor::nat::option")]
    pub last_erc20_scraped_block_number: Option<Nat>,
    #[cbor(n(7), with = "icrc_cbor::principal::option")]
    pub evm_rpc_id: Option<Principal>,
    #[n(8)]
    pub deposit_with_subaccount_helper_contract_address: Option<String>,
    #[cbor(n(9), with = "icrc_cbor::nat::option")]
    pub last_deposit_with_subaccount_scraped_block_number: Option<Nat>,
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L450-538)
```rust
    fn upgrade(&mut self, upgrade_args: UpgradeArg) -> Result<(), InvalidStateError> {
        use std::str::FromStr;

        let UpgradeArg {
            next_transaction_nonce,
            minimum_withdrawal_amount,
            ethereum_contract_address,
            ethereum_block_height,
            ledger_suite_orchestrator_id,
            erc20_helper_contract_address,
            last_erc20_scraped_block_number,
            evm_rpc_id,
            deposit_with_subaccount_helper_contract_address,
            last_deposit_with_subaccount_scraped_block_number,
        } = upgrade_args;
        if let Some(nonce) = next_transaction_nonce {
            let nonce = TransactionNonce::try_from(nonce)
                .map_err(|e| InvalidStateError::InvalidTransactionNonce(format!("ERROR: {e}")))?;
            self.eth_transactions.update_next_transaction_nonce(nonce);
        }
        if let Some(amount) = minimum_withdrawal_amount {
            let minimum_withdrawal_amount = Wei::try_from(amount).map_err(|e| {
                InvalidStateError::InvalidMinimumWithdrawalAmount(format!("ERROR: {e}"))
            })?;
            self.cketh_minimum_withdrawal_amount = minimum_withdrawal_amount;
        }
        if let Some(address) = ethereum_contract_address {
            let eth_helper_contract_address = Address::from_str(&address).map_err(|e| {
                InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e}"))
            })?;
            self.log_scrapings
                .set_contract_address(
                    LogScrapingId::EthDepositWithoutSubaccount,
                    eth_helper_contract_address,
                )
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
        }
        if let Some(address) = erc20_helper_contract_address {
            let erc20_helper_contract_address = Address::from_str(&address).map_err(|e| {
                InvalidStateError::InvalidErc20HelperContractAddress(format!("ERROR: {e}"))
            })?;
            self.log_scrapings
                .set_contract_address(
                    LogScrapingId::Erc20DepositWithoutSubaccount,
                    erc20_helper_contract_address,
                )
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
        }
        if let Some(block_number) = last_erc20_scraped_block_number {
            self.log_scrapings.set_last_scraped_block_number(
                LogScrapingId::Erc20DepositWithoutSubaccount,
                BlockNumber::try_from(block_number).map_err(|e| {
                    InvalidStateError::InvalidLastErc20ScrapedBlockNumber(format!("ERROR: {e}"))
                })?,
            );
        }
        if let Some(address) = deposit_with_subaccount_helper_contract_address {
            let address = Address::from_str(&address).map_err(|e| {
                InvalidStateError::InvalidErc20HelperContractAddress(format!("ERROR: {e}"))
            })?;
            self.log_scrapings
                .set_contract_address(LogScrapingId::EthOrErc20DepositWithSubaccount, address)
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
        }
        if let Some(block_number) = last_deposit_with_subaccount_scraped_block_number {
            self.log_scrapings.set_last_scraped_block_number(
                LogScrapingId::EthOrErc20DepositWithSubaccount,
                BlockNumber::try_from(block_number).map_err(|e| {
                    InvalidStateError::InvalidLastErc20ScrapedBlockNumber(format!("ERROR: {e}"))
                })?,
            );
        }
        if let Some(block_height) = ethereum_block_height {
            self.ethereum_block_height = block_height;
        }
        if let Some(orchestrator_id) = ledger_suite_orchestrator_id {
            self.ledger_suite_orchestrator_id = Some(orchestrator_id);
        }
        if let Some(evm_id) = evm_rpc_id {
            self.evm_rpc_id = evm_id;
        }
        self.validate_config()
    }
```

**File:** rs/ethereum/cketh/minter/src/lifecycle/init.rs (L86-87)
```rust
        let state = Self {
            ethereum_network,
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L255-263)
```rust
        let ethereum_network = read_state(State::ethereum_network);
        let nonce = read_state(|s| s.eth_transactions.next_transaction_nonce());
        let gas_limit = estimate_gas_limit(&request);
        match create_transaction(
            &request,
            nonce,
            gas_fee_estimate.clone(),
            gas_limit,
            ethereum_network,
```
