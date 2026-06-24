Let me look at the withdrawal request structure and how the reimbursement request is created to complete the analysis.

Based on my thorough code review, here is the assessment:

---

### Title
Missing Blocklist Re-check in `process_reimbursement` Allows Post-Sanction ckETH/ckERC20 Minting — (`rs/ethereum/cketh/minter/src/withdraw.rs`)

### Summary

The `process_reimbursement` function mints ckETH/ckERC20 tokens to a user's IC principal without re-checking whether the original Ethereum withdrawal destination is now on the blocklist. Because the blocklist is a compile-time constant updated only via minter upgrades, a window exists where a withdrawal initiated before an address was sanctioned can result in a reimbursement minted after the address is sanctioned.

### Finding Description

The blocklist in `blocklist.rs` is a compile-time constant array updated only when the minter WASM is upgraded via NNS governance: [1](#0-0) 

The blocklist check occurs only at withdrawal initiation time, inside `validate_address_as_destination`, called from `withdraw_eth` and `withdraw_erc20`: [2](#0-1) 

When a transaction fails on Ethereum, `record_finalized_transaction` creates a `ReimbursementRequest` that stores only the IC principal (`to: request.from`) — the Ethereum destination address is **not** preserved: [3](#0-2) 

The `ReimbursementRequest` struct confirms no Ethereum address field exists: [4](#0-3) 

`process_reimbursement` then iterates all pending reimbursements and mints directly to `reimbursement_request.to` (the IC principal) with **no call to `is_blocked`** at any point: [5](#0-4) 

### Impact Explanation

A sanctioned party (whose Ethereum address is added to the blocklist in a minter upgrade) can still receive ckETH or ckERC20 tokens on the IC via the reimbursement path. These tokens can subsequently be used to initiate a new withdrawal to a different, non-blocked Ethereum address, effectively bypassing the OFAC sanctions enforcement that the blocklist is designed to provide.

### Likelihood Explanation

The scenario requires:
1. A user initiates a withdrawal to Ethereum address X (not blocked at that time) — standard unprivileged user action via `withdraw_eth` or `withdraw_erc20`.
2. The Ethereum transaction fails. For ckETH (gas limit 21,000), this occurs naturally when withdrawing to a smart contract address (21,000 gas is insufficient for contract execution). For ckERC20 (gas limit 65,000), contract reverts are also possible.
3. A minter upgrade adds X to the blocklist — a routine, expected operation (the upgrade notes show OFAC blocklist updates happen regularly, e.g., `9c4e4500ea chore(ckbtc/cketh): update ckBTC/ckETH OFAC blocklists 05.2025`).
4. `process_reimbursement` runs on its timer interval and mints without re-checking.

The window between withdrawal initiation and reimbursement processing can span multiple minter upgrades. The attacker is fully unprivileged and the path is concrete and state-machine testable.

### Recommendation

Two complementary fixes:

1. **Store the Ethereum destination address in `ReimbursementRequest`** so it can be re-checked at reimbursement time.
2. **In `process_reimbursement`, call `is_blocked` on the stored destination address** before minting. If the address is now blocked, quarantine the reimbursement (using the existing `record_quarantined_reimbursement` mechanism) rather than minting. [6](#0-5) 

### Proof of Concept

State-machine test outline:
1. Set up `CkEthSetup`, deposit ckETH, approve minter.
2. Call `withdraw_eth` with destination address X (not in blocklist) — accepted.
3. Simulate Ethereum transaction receipt with `TransactionStatus::Failure`.
4. Simulate a minter upgrade that adds X to `ETH_ADDRESS_BLOCKLIST`.
5. Tick the timer to trigger `process_reimbursement`.
6. Assert that ckETH was minted to the user's IC principal despite X now being blocked — confirming the invariant violation.

### Citations

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L17-109)
```rust
const ETH_ADDRESS_BLOCKLIST: &[Address] = &[
    ethereum_address!("0330070FD38Ec3bB94F58FA55D40368271E9e54A"),
    ethereum_address!("04DBA1194ee10112fE6C3207C0687DEf0e78baCf"),
    ethereum_address!("08723392Ed15743cc38513C4925f5e6be5c17243"),
    ethereum_address!("08b2eFdcdB8822EfE5ad0Eae55517cf5DC544251"),
    ethereum_address!("0931cA4D13BB4ba75D9B7132AB690265D749a5E7"),
    ethereum_address!("098B716B8Aaf21512996dC57EB0615e2383E2f96"),
    ethereum_address!("0Ee5067b06776A89CcC7dC8Ee369984AD7Db5e06"),
    ethereum_address!("12de548F79a50D2bd05481C8515C1eF5183666a9"),
    ethereum_address!("1967d8af5bd86a497fb3dd7899a020e47560daaf"),
    ethereum_address!("1999ef52700c34de7ec2b68a28aafb37db0c5ade"),
    ethereum_address!("19aa5fe80d33a56d56c78e82ea5e50e5d80b4dff"),
    ethereum_address!("19F8f2B0915Daa12a3f5C9CF01dF9E24D53794F7"),
    ethereum_address!("1da5821544e25c636c1417ba96ade4cf6d2f9b5a"),
    ethereum_address!("21B8d56BDA776bbE68655A16895afd96F5534feD"),
    ethereum_address!("2f389ce8bd8ff92de3402ffce4691d17fc4f6535"),
    ethereum_address!("308ed4b7b49797e1a98d3818bff6fe5385410370"),
    ethereum_address!("35fB6f6DB4fb05e6A4cE86f2C93691425626d4b1"),
    ethereum_address!("39D908dac893CBCB53Cc86e0ECc369aA4DeF1A29"),
    ethereum_address!("3AD9dB589d201A710Ed237c829c7860Ba86510Fc"),
    ethereum_address!("3cbded43efdaf0fc77b9c55f6fc9988fcc9b757d"),
    ethereum_address!("3Cffd56B47B7b41c56258D9C7731ABaDc360E073"),
    ethereum_address!("3e37627dEAA754090fBFbb8bd226c1CE66D255e9"),
    ethereum_address!("43fa21d92141BA9db43052492E0DeEE5aa5f0A93"),
    ethereum_address!("48549a34ae37b12f6a30566245176994e17c6b4a"),
    ethereum_address!("4f47bc496083c727c5fbe3ce9cdf2b0f6496270c"),
    ethereum_address!("502371699497d08D5339c870851898D6D72521Dd"),
    ethereum_address!("530a64c0ce595026a4a556b703644228179e2d57"),
    ethereum_address!("532b77b33a040587e9fd1800088225f99b8b0e8a"),
    ethereum_address!("53b6936513e738f44FB50d2b9476730C0Ab3Bfc1"),
    ethereum_address!("5512d943ed1f7c8a43f3435c85f7ab68b30121b0"),
    ethereum_address!("57EC89A0C056163A0314e413320f9B3ABe761259"),
    ethereum_address!("5A14E72060c11313E38738009254a90968F58f51"),
    ethereum_address!("5a7a51bfb49f190e5a6060a5bc6052ac14a3b59f"),
    ethereum_address!("5d5b5dafecbf31bdb08bfd3edad4f2694372d0ef"),
    ethereum_address!("5f48c2a71b2cc96e3f0ccae4e39318ff0dc375b2"),
    ethereum_address!("67d40EE1A85bf4a4Bb7Ffae16De985e8427B6b45"),
    ethereum_address!("6be0ae71e6c41f2f9d0d1a3b8d0f75e6f6a0b46e"),
    ethereum_address!("6f1ca141a28907f78ebaa64fb83a9088b02a8352"),
    ethereum_address!("72a5843cc08275C8171E582972Aa4fDa8C397B2A"),
    ethereum_address!("747AFB5c7A7fc34B547cD0FDEbf9b91759C5a52b"),
    ethereum_address!("76EA76CA4Eb727f18956aB93445a94c5280412B9"),
    ethereum_address!("797d7ae72ebddcdea2a346c1834e04d1f8df102b"),
    ethereum_address!("7CEd75026204aC29C34bEA98905D4C949F27361e"),
    ethereum_address!("7Db418b5D567A4e0E8c59Ad71BE1FcE48f3E6107"),
    ethereum_address!("7F19720A857F834887FC9A7bC0a0fBe7Fc7f8102"),
    ethereum_address!("7F367cC41522cE07553e823bf3be79A889DEbe1B"),
    ethereum_address!("7FF9cFad3877F21d41Da833E2F775dB0569eE3D9"),
    ethereum_address!("83E5bC4Ffa856BB84Bb88581f5Dd62A433A25e0D"),
    ethereum_address!("8576acc5c05d6ce88f4e49bf65bdf0c62f91353c"),
    ethereum_address!("8Dce2aAC0dE82bdCAf6b4373B79f94331b8e4995"),
    ethereum_address!("901bb9583b24d97e995513c6778dc6888ab6870e"),
    ethereum_address!("931546D9e66836AbF687d2bc64B30407bAc8C568"),
    ethereum_address!("95584C303FCd48AF5c6B9873015f2AD0ca84EaE3"),
    ethereum_address!("961c5be54a2ffc17cf4cb021d863c42dacd47fc1"),
    ethereum_address!("97b1043abd9e6fc31681635166d430a458d14f9c"),
    ethereum_address!("983a81ca6FB1e441266D2FbcB7D8E530AC2E05A2"),
    ethereum_address!("9Be599d7867f5E1a2D7Ec6dB9710dF2b98A15573"),
    ethereum_address!("9c2bc757b66f24d60f016b6237f8cdd414a879fa"),
    ethereum_address!("9f4cda013e354b8fc285bf4b9a60460cee7f7ea9"),
    ethereum_address!("a0e1c89Ef1a489c9C7dE96311eD5Ce5D32c20E4B"),
    ethereum_address!("a7e5d5a720f06526557c513402f2e6b5fa20b008"),
    ethereum_address!("b338962B92CD818D6aef0A32a9ECD01212a71f33"),
    ethereum_address!("b637f84b66876ebf609c2a4208905f9ddac9d075"),
    ethereum_address!("b6f5ec1a0a9cd1526536d3f0426c429529471f40"),
    ethereum_address!("c103b7dc095c904b92081eef0c1640081ec01c10"),
    ethereum_address!("c2a3829F459B3Edd87791c74cD45402BA0a20Be3"),
    ethereum_address!("c455f7fd3e0e12afd51fba5c106909934d8a0e4a"),
    ethereum_address!("cB74874f1e06Fcf80A306e06e5379A44B488bA2D"),
    ethereum_address!("d04E33461FEA8302c5E1e13895b60cEe8AEfda7F"),
    ethereum_address!("d0975b32cea532eadddfc9c60481976e39db3472"),
    ethereum_address!("d5ED34b52AC4ab84d8FA8A231a3218bbF01Ed510"),
    ethereum_address!("D8500C631dC32FA18645B7436344a99E4825e10e"),
    ethereum_address!("d882cfc20f52f2599d84b8e8d58c7fb62cfe344b"),
    ethereum_address!("db2720ebad55399117ddb4c4a4afd9a4ccada8fe"),
    ethereum_address!("dcbEfFBECcE100cCE9E4b153C4e15cB885643193"),
    ethereum_address!("e1d865c3d669dcc8c57c8d023140cb204e672ee4"),
    ethereum_address!("e1e4c5e5ed8f03ae61b581e2def126025f2b9401"),
    ethereum_address!("e3d35f68383732649669aa990832e017340dbca5"),
    ethereum_address!("e7aa314c77f4233c18c6cc84384a9247c0cf367b"),
    ethereum_address!("E950DC316b836e4EeFb8308bf32Bf7C72a1358FF"),
    ethereum_address!("ed6e0a7e4ac94d976eebfb82ccf777a3c6bad921"),
    ethereum_address!("EFE301d259F525cA1ba74A7977b80D5b060B3ccA"),
    ethereum_address!("f3701f445b6bdafedbca97d1e477357839e4120d"),
    ethereum_address!("f4377edA661e04B6DDA78969796Ed31658D602D4"),
    ethereum_address!("F7B31119c2682c88d88D455dBb9d5932c65Cf1bE"),
    ethereum_address!("Fb3eFf152ea55D1BfA04Dbdd509A80fD7b72cdEB"),
    ethereum_address!("Fda1Ec4A6178d4916b001a065422D31EBE5F62FF"),
];

pub fn is_blocked(address: &Address) -> bool {
    ETH_ADDRESS_BLOCKLIST.binary_search(address).is_ok()
}
```

**File:** rs/ethereum/cketh/minter/src/address.rs (L47-56)
```rust
pub fn validate_address_as_destination(address: &str) -> Result<Address, AddressValidationError> {
    let address =
        Address::from_str(address).map_err(|e| AddressValidationError::Invalid { error: e })?;
    if address == Address::ZERO {
        return Err(AddressValidationError::NotSupported(address));
    }
    if crate::blocklist::is_blocked(&address) {
        return Err(AddressValidationError::Blocked(address));
    }
    Ok(address)
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L236-253)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Decode, Encode)]
pub struct ReimbursementRequest {
    /// Burn index on the ledger that should be reimbursed.
    #[cbor(n(0), with = "crate::cbor::id")]
    pub ledger_burn_index: LedgerBurnIndex,
    /// The amount that should be reimbursed in the smallest denomination.
    #[n(1)]
    pub reimbursed_amount: CkTokenAmount,
    #[cbor(n(2), with = "icrc_cbor::principal")]
    pub to: Principal,
    #[n(3)]
    pub to_subaccount: Option<LedgerSubaccount>,
    /// Transaction hash of the failed ETH transaction.
    /// We use this hash to link the mint reimbursement transaction
    /// on the ledger with the failed ETH transaction.
    #[n(4)]
    pub transaction_hash: Option<Hash>,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L719-731)
```rust
            WithdrawalRequest::CkEth(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index,
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            reimbursed_amount: finalized_tx.transaction_amount().change_units(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L772-779)
```rust
    /// Quarantine the reimbursement request identified by its index to prevent double minting.
    /// WARNING!: It's crucial that this method does not panic,
    /// since it's called inside the clean-up callback, when an unexpected panic did occur before.
    pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
        self.reimbursement_requests.remove(&index);
        self.reimbursed
            .insert(index, Err(ReimbursedError::Quarantined));
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L67-95)
```rust
    for (index, reimbursement_request) in reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
            mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
        });
        let ledger_canister_id = match index {
            ReimbursementIndex::CkEth { .. } => read_state(|s| s.cketh_ledger_id),
            ReimbursementIndex::CkErc20 { ledger_id, .. } => ledger_id,
        };
        let client = ICRC1Client {
            runtime: CdkRuntime,
            ledger_canister_id,
        };
        let memo = Memo::from(reimbursement_request.clone());
        let args = TransferArg {
            from_subaccount: None,
            to: Account {
                owner: reimbursement_request.to,
                subaccount: reimbursement_request
                    .to_subaccount
                    .map(LedgerSubaccount::to_bytes),
            },
            fee: None,
            created_at_time: None,
            memo: Some(memo),
            amount: Nat::from(reimbursement_request.reimbursed_amount),
        };
        let block_index = match client.transfer(args).await {
```
