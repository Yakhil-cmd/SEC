### Title
Missing Mechanism to Remove a Supported ckERC20 Token from the Minter's Whitelist - (`rs/ethereum/cketh/minter/src/main.rs`, `rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter's `ckerc20_tokens` mapping is append-only. Once an ERC-20 token is added via `add_ckerc20_token`, there is no endpoint or governance argument to remove it. If a supported ERC-20 token is compromised (e.g., infinite-mint exploit) or depegs severely, the minter will continue to scrape Ethereum logs for that token and mint ckERC20 tokens indefinitely. The only remediation path is a full NNS canister upgrade, which takes days to pass, leaving the protocol exposed during the response window.

---

### Finding Description

The ckETH minter maintains a `DedupMultiKeyMap` called `ckerc20_tokens` that maps `(ledger_id, erc20_contract_address) → token_symbol` for all supported ckERC20 tokens. [1](#0-0) 

Tokens are added to this map exclusively through the `add_ckerc20_token` update endpoint, which is restricted to the ledger suite orchestrator canister: [2](#0-1) 

The state mutation function `record_add_ckerc20_token` only inserts into the map; there is no corresponding `record_remove_ckerc20_token`: [3](#0-2) 

The minter's Candid interface exposes `add_ckerc20_token` but has no `remove_ckerc20_token` or `pause_ckerc20_token` endpoint: [4](#0-3) 

The ledger suite orchestrator's `OrchestratorArg` enum only has `InitArg`, `UpgradeArg`, and `AddErc20Arg` variants — no `RemoveErc20Arg`: [5](#0-4) 

This is mirrored in the orchestrator's Candid interface: [6](#0-5) 

The `withdraw_erc20` endpoint checks whether a token is currently in `ckerc20_tokens` before proceeding, but since removal is impossible, this check can never return false for a previously added token: [7](#0-6) 

---

### Impact Explanation

If a supported ERC-20 token (e.g., ckUSDC, ckUSDT, ckWBTC) suffers an infinite-mint exploit on Ethereum, an attacker can:

1. Mint unlimited ERC-20 tokens on Ethereum at zero cost.
2. Deposit them to the minter's helper contract, triggering `ReceivedEthOrErc20` log events.
3. The minter scrapes these logs on its timer and calls `record_event_to_mint`, which asserts the ERC-20 address is in `ckerc20_tokens` — it is, so minting proceeds: [8](#0-7) 
4. Unlimited ckERC20 tokens are minted on ICP and can be injected into ICP DeFi protocols, draining real assets from liquidity pools that accept ckERC20 as collateral.

Even in a depeg scenario (as happened with USDC in March 2023), the minter continues to accept deposits and process withdrawals for the depegged token with no per-token halt capability. The only response is a full NNS canister upgrade, which requires a governance vote that takes a minimum of 4 days under normal conditions.

---

### Likelihood Explanation

- USDC, USDT, WBTC, SHIB, EURC, and wstETH are all currently supported ckERC20 tokens on mainnet. Several of these have experienced depeg events or smart contract vulnerabilities historically.
- The Ethereum helper contract explicitly does not enforce a whitelist of ERC-20 tokens: [9](#0-8) 
- Any user can call `depositErc20` on the Ethereum helper contract with any ERC-20 address. The minter's only filter is the `ckerc20_tokens` map, which cannot be updated to remove a token.
- The attack requires an external ERC-20 compromise as a trigger, but the IC code's missing removal mechanism is the necessary vulnerable step that prevents a timely response.

---

### Recommendation

1. **Add a `remove_ckerc20_token` endpoint** to the minter, callable only by the orchestrator (or NNS directly), that removes a token from `ckerc20_tokens` and stops log scraping for its contract address.

2. **Add a `RemoveErc20Arg` variant** to `OrchestratorArg` in the ledger suite orchestrator so that token removal can be triggered via an NNS upgrade proposal to the orchestrator, which then calls the minter.

3. **Add a per-token pause mechanism** as a faster emergency response: a separate `paused_ckerc20_tokens` set that the minter checks before minting or processing withdrawals, allowing the NNS to halt a specific token without a full canister upgrade.

4. **Check token support at withdrawal time** against the live (possibly paused) state, not just at request acceptance time, so that in-flight withdrawal requests for a paused token can be rejected and ckETH gas fees reimbursed.

---

### Proof of Concept

**Scenario: Infinite-mint exploit on a supported ERC-20**

1. Attacker exploits the USDC contract (or any supported ERC-20) to mint `N` tokens to their Ethereum address.
2. Attacker calls `depositErc20(usdc_address, N, attacker_ic_principal, 0x)` on the minter's ERC-20 helper contract. The helper contract emits `ReceivedEthOrErc20`.
3. On the next minter timer tick, `scrape_eth_logs` fetches the log. The minter calls `record_event_to_mint`, which checks:
   ```rust
   assert!(self.ckerc20_tokens.contains_alt(&event.erc20_contract_address), ...)
   ```
   This passes because USDC is in `ckerc20_tokens` and cannot be removed. [8](#0-7) 
4. The minter mints `N` ckUSDC to the attacker's ICP principal.
5. The attacker uses ckUSDC in ICP DeFi protocols to drain real assets.
6. The NNS must pass an emergency upgrade proposal to the minter canister to stop further minting — a process that takes days — during which the attacker can repeat steps 1–5 indefinitely. [2](#0-1) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L98-103)
```rust
    /// ERC-20 tokens that the minter can mint:
    /// - primary key: ledger ID for the ckERC20 token
    /// - secondary key: ERC-20 contract address on Ethereum
    /// - value: ckERC20 token symbol
    pub ckerc20_tokens: DedupMultiKeyMap<Principal, Address, CkTokenSymbol>,
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L199-205)
```rust
        if let ReceivedEvent::Erc20(event) = event {
            assert!(
                self.ckerc20_tokens
                    .contains_alt(&event.erc20_contract_address),
                "BUG: unsupported ERC-20 contract address in event {event:?}"
            )
        }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L398-424)
```rust
    pub fn record_add_ckerc20_token(&mut self, ckerc20_token: CkErc20Token) {
        assert_eq!(
            self.ethereum_network, ckerc20_token.erc20_ethereum_network,
            "ERROR: Expected {}, but got {}",
            self.ethereum_network, ckerc20_token.erc20_ethereum_network
        );
        let ckerc20_with_same_symbol = self
            .supported_ck_erc20_tokens()
            .filter(|ckerc20| ckerc20.ckerc20_token_symbol == ckerc20_token.ckerc20_token_symbol)
            .collect::<Vec<_>>();
        assert_eq!(
            ckerc20_with_same_symbol,
            vec![],
            "ERROR: ckERC20 token symbol {} is already used by {:?}",
            ckerc20_token.ckerc20_token_symbol,
            ckerc20_with_same_symbol
        );
        assert_eq!(
            self.ckerc20_tokens.try_insert(
                ckerc20_token.ckerc20_ledger_id,
                ckerc20_token.erc20_contract_address,
                ckerc20_token.ckerc20_token_symbol,
            ),
            Ok(()),
            "ERROR: some ckERC20 tokens use the same ckERC20 ledger ID or ERC-20 address"
        );
    }
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L562-574)
```rust
#[update]
async fn add_ckerc20_token(erc20_token: AddCkErc20Token) {
    let orchestrator_id = read_state(|s| s.ledger_suite_orchestrator_id)
        .unwrap_or_else(|| ic_cdk::trap("ERROR: ERC-20 feature is not activated"));
    if orchestrator_id != ic_cdk::api::msg_caller() {
        ic_cdk::trap(format!(
            "ERROR: only the orchestrator {orchestrator_id} can add ERC-20 tokens"
        ));
    }
    let ckerc20_token = erc20::CkErc20Token::try_from(erc20_token)
        .unwrap_or_else(|e| ic_cdk::trap(format!("ERROR: {e}")));
    mutate_state(|s| process_event(s, EventType::AddedCkErc20Token(ckerc20_token)));
}
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L744-750)
```text
    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();

    // Decode ledger memos produced by the minter when minting (deposits) or burning (withdrawals).
    decode_ledger_memo : (DecodeLedgerMemoArgs) -> (DecodeLedgerMemoResult) query;
}
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

**File:** rs/ethereum/ledger-suite-orchestrator/ledger_suite_orchestrator.did (L1-5)
```text
type OrchestratorArg = variant {
    UpgradeArg : UpgradeArg;
    InitArg : InitArg;
    AddErc20Arg : AddErc20Arg;
};
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L182-191)
```text
[WARNING]
.Supported ERC-20 tokens
====
Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens. Therefore, funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it. To avoid any loss of funds, please verify **before** any important transfer that the desired ERC-20 token is supported by querying the minter as follows
and checking the field `supported_ckerc20_tokens`:
[source,shell]
----
dfx canister --network ic call minter get_minter_info
----
====
```
