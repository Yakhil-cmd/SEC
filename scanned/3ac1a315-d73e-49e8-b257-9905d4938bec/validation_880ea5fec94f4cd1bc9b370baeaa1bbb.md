### Title
Missing Reentrancy Guard in `perform_mint_sns_tokens` Allows Double-Minting of SNS Tokens via Canister Upgrade Race - (File: rs/sns/governance/src/governance.rs)

### Summary
`perform_mint_sns_tokens` in SNS governance makes an inter-canister call to the ledger (`transfer_funds(...).await`) without first marking the proposal as "in-progress" or acquiring any reentrancy guard. During the await checkpoint, the governance canister can process other messages — including a canister upgrade — which drops the in-flight callback and leaves the proposal in `Adopted` state. On the next heartbeat, the governance canister re-executes the still-adopted proposal, minting SNS tokens a second time.

### Finding Description

`perform_mint_sns_tokens` in `rs/sns/governance/src/governance.rs` is the execution handler for `MintSnsTokens` governance proposals. It directly calls `self.ledger.transfer_funds(...).await` — an inter-canister call to the SNS ledger — without any guard to prevent re-execution:

```rust
async fn perform_mint_sns_tokens(
    &mut self,
    mint: MintSnsTokens,
) -> Result<(), GovernanceError> {
    // ... build `to` and `amount_e8s` ...
    self.ledger
        .transfer_funds(amount_e8s, 0, None, to, mint.memo())
        .await?;          // <-- inter-canister call; no guard before this
    Ok(())
}
```

The proposal's execution status (`executed_timestamp_seconds`) is only written **after** `perform_mint_sns_tokens` returns, inside `set_proposal_execution_status`. During the `await`, the proposal remains in `Adopted` state.

The IC's async model means that between any two `await` points, other messages — including heartbeats and canister upgrades — can be processed. If a canister upgrade is applied while `transfer_funds` is in-flight, the background task is silently dropped. The proposal stays `Adopted`. On the next heartbeat, `run_periodic_tasks` re-executes all adopted proposals, calling `perform_mint_sns_tokens` again and issuing a second `transfer_funds` to the ledger.

This contrasts sharply with the explicit double-minting protections used elsewhere in the same codebase:
- `ckBTC` minter uses `scopeguard` before every `mint_ckbtc(...).await` call.
- `ckETH` minter uses `scopeguard` before every `transfer(...).await` call.
- NNS governance `mint_monthly_node_provider_rewards` acquires a `LOCK` before any inter-canister call.

`perform_mint_sns_tokens` has none of these protections.

### Impact Explanation

An adopted `MintSnsTokens` proposal can be executed more than once, minting SNS tokens to the target account multiple times. This inflates the SNS token supply beyond what governance approved, violating ledger conservation. The minted tokens are immediately spendable by the recipient. This is a **chain-fusion mint/burn/replay bug** with direct financial impact on every SNS-governed token.

### Likelihood Explanation

SNS canisters are routinely upgraded via governance proposals. A `MintSnsTokens` proposal and an `UpgradeSnsControlledCanister` proposal can be adopted in the same voting window. If the upgrade is applied while the mint's `transfer_funds` call is in-flight (a window of one or more consensus rounds), the double-mint condition is triggered. No privileged key or threshold corruption is required — only two concurrently adopted governance proposals, which is a normal operational scenario. An adversary holding sufficient SNS voting power can deliberately time both proposals to maximize the overlap window.

### Recommendation

Before calling `transfer_funds(...).await`, mark the proposal as "in-progress" in persistent state (e.g., set a `mint_in_progress` flag keyed by proposal ID), or use a `scopeguard` that quarantines the proposal on panic/drop, consistent with the pattern already used in `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` and `rs/ethereum/cketh/minter/src/deposit.rs`. The guard must be set **before** the first `await` point so that a canister upgrade cannot reset it.

### Proof of Concept

**Vulnerable function** — no guard before inter-canister call: [1](#0-0) 

**Call site** — proposal execution dispatched as a background task with no in-progress marker: [2](#0-1) 

**Proposal status only written after await returns** — leaving `Adopted` state exposed during the inter-canister call: [3](#0-2) 

**Contrast: ckBTC minter uses `scopeguard` before every `mint_ckbtc(...).await`:** [4](#0-3) 

**Contrast: ckETH minter uses `scopeguard` before every `transfer(...).await`:** [5](#0-4) 

**Contrast: NNS governance acquires a `LOCK` before inter-canister calls in its minting path:** [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2212-2212)
```rust
            Action::MintSnsTokens(mint) => self.perform_mint_sns_tokens(mint).await,
```

**File:** rs/sns/governance/src/governance.rs (L2240-2242)
```rust
        };

        self.set_proposal_execution_status(proposal_id, result);
```

**File:** rs/sns/governance/src/governance.rs (L3062-3088)
```rust
    async fn perform_mint_sns_tokens(
        &mut self,
        mint: MintSnsTokens,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: mint
                .to_principal
                .ok_or(GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    "Expected mint to have a target principal",
                ))?
                .0,
            subaccount: mint
                .to_subaccount
                .as_ref()
                .map(|s| bytes_to_subaccount(&s.subaccount[..]))
                .transpose()?,
        };
        let amount_e8s = mint.amount_e8s.ok_or(GovernanceError::new_with_message(
            ErrorType::InvalidProposal,
            "Expected MintSnsTokens to have an an amount_e8s",
        ))?;
        self.ledger
            .transfer_funds(amount_e8s, 0, None, to, mint.memo())
            .await?;
        Ok(())
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L333-341)
```rust
        let guard = scopeguard::guard((utxo.clone(), caller_account), |(utxo, account)| {
            mutate_state(|s| {
                state::audit::mark_utxo_checked_mint_unknown(s, utxo, account, runtime)
            });
        });

        match runtime
            .mint_ckbtc(amount, caller_account, crate::memo::encode(&memo).into())
            .await
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L43-52)
```rust
        let prevent_double_minting_guard = scopeguard::guard(event.clone(), |event| {
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::QuarantinedDeposit {
                        event_source: event.source(),
                    },
                )
            });
        });
```

**File:** rs/nns/governance/src/governance.rs (L4042-4065)
```rust
        thread_local! {
            static LOCK: RefCell<Option<u64>> = const { RefCell::new(None) };
        }
        let release_on_drop = ic_nervous_system_lock::acquire(&LOCK, self.env.now());
        if let Err(earlier_call_start_timestamp) = release_on_drop {
            // Log, but not too frequently (at most once every 5 minutes).
            thread_local! {
                static LAST_LOGGED_UNAVAILABLE_TIMESTAMP_SECONDS: RefCell<u64> = const { RefCell::new(0) };
            }
            let time_since_logged_seconds = LAST_LOGGED_UNAVAILABLE_TIMESTAMP_SECONDS
                .with(|t| self.env.now().saturating_sub(*t.borrow()));
            if time_since_logged_seconds > 5 * 60 {
                println!(
                    "{}Another mint_monthly_node_provider_rewards call (started at \
                     {} seconds since the UNIX epoch) is already in progress.",
                    LOG_PREFIX, earlier_call_start_timestamp,
                );
                LAST_LOGGED_UNAVAILABLE_TIMESTAMP_SECONDS.with(|t| {
                    *t.borrow_mut() = self.env.now();
                });
            }

            return Ok(());
        }
```
