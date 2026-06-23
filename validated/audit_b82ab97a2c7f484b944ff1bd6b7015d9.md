### Title
ckERC20 ERC-20 Contract Address Is Permanently Bound With No Migration Path, Causing ckERC20 Tokens to Become Unbacked on Token Migration - (File: rs/ethereum/cketh/minter/src/state.rs, rs/ethereum/ledger-suite-orchestrator/ledger_suite_orchestrator.did)

---

### Summary

The ckERC20 chain-fusion system permanently binds each ckERC20 token to a specific Ethereum ERC-20 contract address at registration time. Neither the Ledger Suite Orchestrator nor the ckETH minter exposes any on-chain mechanism to update or remove that binding. If an ERC-20 token issuer migrates to a new contract address (a real historical pattern: DAI v1→v2, USDT upgrades, USDC v1→v2), the minter continues sending withdrawal transactions to the old, now-deprecated contract, and all ckERC20 tokens held on IC become permanently unbacked.

---

### Finding Description

When a ckERC20 token is added via an NNS upgrade proposal, the Ledger Suite Orchestrator calls `lifecycle::add_erc20`, which stores the `(chain_id, address)` pair as the permanent identifier for that token. The minter receives this via `add_ckerc20_token` and stores it in `ckerc20_tokens: DedupMultiKeyMap<Principal, Address, CkTokenSymbol>`.

The `OrchestratorArg` enum has exactly three variants — `InitArg`, `UpgradeArg`, `AddErc20Arg` — with no `UpdateErc20Arg` or `RemoveErc20Arg`: [1](#0-0) [2](#0-1) 

The minter's `record_add_ckerc20_token` function explicitly panics if any attempt is made to re-register the same ledger ID or ERC-20 address, confirming there is no update path: [3](#0-2) 

The `add_ckerc20_token` endpoint on the minter is restricted to the orchestrator and is add-only: [4](#0-3) 

When a user calls `withdraw_erc20`, the minter looks up the stored `erc20_contract_address` from `ckerc20_tokens` and embeds it directly into the Ethereum transaction: [5](#0-4) [6](#0-5) 

If the underlying ERC-20 token has migrated to a new contract address, this transaction is sent to the old (deprecated) contract. The Ethereum transaction may succeed on-chain (the old contract may still accept calls), but the user receives worthless tokens from the deprecated contract. The ckERC20 tokens on IC remain in circulation but are no longer backed by real ERC-20 value.

---

### Impact Explanation

**Ledger conservation break / chain-fusion unbacking**: All ckERC20 token holders on IC lose the 1:1 backing guarantee. Withdrawal transactions are routed to the old ERC-20 contract address. Deposits to the new contract address are not recognized by the minter (it only scrapes logs for the registered old address). The ckERC20 ledger continues to record balances, but those balances cannot be redeemed for the new token. The minter's `erc20_balances` accounting also becomes stale, as it tracks balances at the old contract address. [7](#0-6) [8](#0-7) 

---

### Likelihood Explanation

ERC-20 token migrations are rare but precedented: DAI migrated from SAI (v1) to DAI (v2) in 2019; USDT and USDC have undergone contract upgrades; WBTC and other tokens have had governance-driven migrations. The IC currently supports ckUSDC, ckUSDT, ckWBTC, ckLINK, ckUNI, ckSHIB, ckWSTETH, ckXAUT — all high-value tokens whose issuers have historically upgraded contracts. The likelihood of at least one migration event over the multi-year lifetime of the ckERC20 system is non-trivial. Impact when it occurs is total loss of backing for all holders of the affected ckERC20 token.

---

### Recommendation

**Short-term**: Document this limitation explicitly in `rs/ethereum/cketh/docs/ckerc20.adoc` and `rs/ethereum/ledger-suite-orchestrator/README.adoc`. Warn users that ckERC20 tokens are permanently bound to the ERC-20 contract address registered at creation time and that token migrations are not automatically followed.

**Long-term**: Add an `UpdateErc20Arg` variant to `OrchestratorArg` that allows an NNS proposal to update the `erc20_contract_address` binding in both the orchestrator state and the minter's `ckerc20_tokens` map. This update should: (1) pause new deposits/withdrawals for the affected token, (2) update the contract address in both canisters atomically, (3) resume scraping from the new contract address at the appropriate block number. Alternatively, document an off-chain emergency procedure (e.g., coordinating with the token issuer to maintain a migration wrapper contract that forwards calls to the new contract).

---

### Proof of Concept

1. NNS passes a proposal adding ckUSDC with contract address `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` (current USDC on Ethereum mainnet).
2. Users deposit USDC and receive ckUSDC on IC. The minter holds real USDC at the old contract.
3. Circle deploys USDC v2 at a new address `0xNEWADDRESS` and deprecates `0xA0b86991...` (as happened with USDC v1→v2 on other chains).
4. A user calls `withdraw_erc20` with their ckUSDC. The minter looks up `ckerc20_tokens` and finds `erc20_contract_address = 0xA0b86991...`.
5. The minter constructs and signs an Ethereum transaction calling `transfer(destination, amount)` on `0xA0b86991...` (the old contract). The transaction succeeds on-chain but delivers deprecated/worthless USDC v1 tokens.
6. The user's ckUSDC is burned on IC, but they receive no real value on Ethereum.
7. There is no `UpdateErc20Arg` variant in `OrchestratorArg` to fix the binding: [1](#0-0) 

8. Any attempt to re-register the same ledger ID or ERC-20 address via `AddErc20Arg` panics in `record_add_ckerc20_token`: [3](#0-2)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/ledger_suite_orchestrator.did (L1-5)
```text
type OrchestratorArg = variant {
    UpgradeArg : UpgradeArg;
    InitArg : InitArg;
    AddErc20Arg : AddErc20Arg;
};
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/candid/mod.rs (L7-12)
```rust
#[derive(Clone, Debug, CandidType, Deserialize)]
pub enum OrchestratorArg {
    InitArg(InitArg),
    UpgradeArg(UpgradeArg),
    AddErc20Arg(AddErc20Arg),
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L74-77)
```rust
    /// Current balance of ERC-20 tokens held by the minter.
    /// Computed based on audit events.
    pub erc20_balances: Erc20Balances,

```

**File:** rs/ethereum/cketh/minter/src/state.rs (L98-103)
```rust
    /// ERC-20 tokens that the minter can mint:
    /// - primary key: ledger ID for the ckERC20 token
    /// - secondary key: ERC-20 contract address on Ethereum
    /// - value: ckERC20 token symbol
    pub ckerc20_tokens: DedupMultiKeyMap<Principal, Address, CkTokenSymbol>,
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L415-423)
```rust
        assert_eq!(
            self.ckerc20_tokens.try_insert(
                ckerc20_token.ckerc20_ledger_id,
                ckerc20_token.erc20_contract_address,
                ckerc20_token.ckerc20_token_symbol,
            ),
            Ok(()),
            "ERROR: some ckERC20 tokens use the same ckERC20 ledger ID or ERC-20 address"
        );
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L744-746)
```text
    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L418-428)
```rust
    let ckerc20_token = read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id))
        .ok_or_else(|| {
            let supported_ckerc20_tokens: BTreeSet<_> = read_state(|s| {
                s.supported_ck_erc20_tokens()
                    .map(|token| token.into())
                    .collect()
            });
            WithdrawErc20Error::TokenNotSupported {
                supported_tokens: Vec::from_iter(supported_ckerc20_tokens),
            }
        })?;
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L480-492)
```rust
                    let withdrawal_request = Erc20WithdrawalRequest {
                        max_transaction_fee: erc20_tx_fee,
                        withdrawal_amount: ckerc20_withdrawal_amount,
                        destination,
                        cketh_ledger_burn_index,
                        ckerc20_ledger_id: ckerc20_token.ckerc20_ledger_id,
                        ckerc20_ledger_burn_index,
                        erc20_contract_address: ckerc20_token.erc20_contract_address,
                        from: caller,
                        from_subaccount: from_ckerc20_subaccount
                            .and_then(LedgerSubaccount::from_bytes),
                        created_at: now,
                    };
```
