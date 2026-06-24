### Title
SNS Treasury Manager DEX Deposit Can Be Sandwich-Attacked Between ICRC-2 Approval and Deposit Call - (File: rs/sns/governance/src/extensions.rs)

### Summary

The SNS governance proposal execution for `ExecuteExtensionOperation` (TreasuryManagerDeposit) and `RegisterExtension` performs ICRC-2 approval of treasury funds in one async step, then calls the treasury manager's `deposit` endpoint in a separate async step. Between these two steps, any unprivileged canister or user can manipulate the DEX pool price, causing the SNS treasury to receive fewer LP tokens than expected at the time the proposal was voted on.

### Finding Description

In `execute_treasury_manager_deposit()` (`rs/sns/governance/src/extensions.rs`), the execution is split across two sequential `await` points: [1](#0-0) 

Step 1 calls `approve_treasury_manager()`, which issues two `icrc2_approve` calls — one on the SNS ledger and one on the ICP ledger — granting the treasury manager canister a time-limited allowance: [2](#0-1) 

Step 2 calls `deposit` on the treasury manager canister, which then interacts with the external DEX: [3](#0-2) 

The same two-step pattern exists in `ValidatedRegisterExtension::execute()`, where `approve_treasury_manager` is awaited before `upgrade_non_root_canister` (which triggers the treasury manager's `init`, which deposits to the DEX): [4](#0-3) 

Because the IC's async execution model allows other canister messages to be processed between any two `await` points, a malicious actor can observe the ICRC-2 approval transactions on the public ledger and immediately submit a large swap on the DEX to skew the pool price ratio before the treasury manager's `deposit` call executes.

The codebase itself acknowledges this risk in two places. The treasury manager DID file states: [5](#0-4) 

And the proposal rendering function for `RegisterExtension` includes an explicit warning: [6](#0-5) 

Neither location provides a protocol-level mitigation — no minimum LP token output is enforced, and no slippage bound is validated anywhere in the governance execution path.

### Impact Explanation

An unprivileged attacker can cause the SNS treasury to deposit ICP and SNS tokens into a DEX liquidity pool at a manipulated price ratio, receiving significantly fewer LP tokens than the SNS community voted to receive. The attacker profits by sandwiching the deposit: they first skew the pool price (e.g., by dumping one token), then the treasury deposits at the bad price, then the attacker reverses their position. The SNS treasury permanently loses value — the difference between the expected and actual LP tokens received — with no recourse, since the deposit is irreversible once executed.

### Likelihood Explanation

The attack is realistic and low-cost:

1. ICRC-2 approval transactions are publicly visible on the ICP and SNS ledgers immediately after `approve_treasury_manager` completes.
2. The IC's async model guarantees that other canister messages (including DEX swaps) can be interleaved between the approval `await` and the `deposit` `await`.
3. Any canister or user with sufficient tokens to move the DEX pool price can execute this attack — no privileged access is required.
4. The attack is economically motivated: the attacker extracts the price impact as profit.

The only friction is the need to hold enough tokens to meaningfully move the DEX pool, but for large SNS treasury deposits this is a realistic threshold for well-capitalized actors.

### Recommendation

1. **Enforce a minimum LP token output** in the `ExecuteExtensionOperation` proposal arguments. The SNS community should vote on an acceptable slippage bound (e.g., minimum LP tokens to receive), and the governance execution should verify this bound is met after the deposit call returns.
2. **Validate the deposit result** against the pre-approved token amounts. If the treasury manager returns balances indicating the deposit was executed at a ratio significantly different from the approved amounts, the governance canister should revert (withdraw) and fail the proposal.
3. **Reduce the ICRC-2 approval window**: the current expiry is `now + ONE_HOUR_SECONDS`, which gives a large window for observation and attack. A tighter expiry or a zero-expiry (single-use approval) would reduce the attack surface.
4. **Document the minimum slippage requirement** in the `RegisterExtension` and `ExecuteExtensionOperation` proposal validation, so SNS communities are aware they must specify slippage bounds.

### Proof of Concept

1. An SNS community passes an `ExecuteExtensionOperation` (TreasuryManagerDeposit) proposal to deposit X SNS tokens and Y ICP into a KongSwap pool.
2. The proposal executes: `approve_treasury_manager` is called, setting ICRC-2 allowances on both ledgers. These approval transactions are immediately visible on the public ledgers.
3. An attacker observes the approval and immediately submits a large swap on the KongSwap pool (e.g., selling SNS tokens for ICP), skewing the pool ratio.
4. The governance canister's next `await` resumes and calls `deposit` on the treasury manager. The treasury manager calls KongSwap's deposit with the approved amounts. Because the pool ratio is now skewed, the SNS treasury receives far fewer LP tokens than expected.
5. The attacker reverses their swap, restoring the pool ratio and pocketing the arbitrage profit. The SNS treasury has permanently lost value with no on-chain recourse. [1](#0-0) [2](#0-1) [5](#0-4)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L545-564)
```rust
                    governance
                        .approve_treasury_manager(
                            extension_canister_id,
                            treasury_allocation_sns_e8s,
                            treasury_allocation_icp_e8s,
                        )
                        .await?;

                    init_blob
                }
            };

            governance
                .upgrade_non_root_canister(
                    extension_canister_id,
                    wasm,
                    init_blob,
                    CanisterInstallMode::Install,
                )
                .await?;
```

**File:** rs/sns/governance/src/extensions.rs (L777-831)
```rust
    async fn approve_treasury_manager(
        &self,
        treasury_manager_canister_id: CanisterId,
        sns_amount_e8s: u64,
        icp_amount_e8s: u64,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: treasury_manager_canister_id.get().0,
            subaccount: None,
        };

        let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
        let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);

        // If expected_allowance is None, the ledger *blindly* overwrites any existing
        // allowance (even if non-zero). Therefore, there is no risk of double spending.

        self.ledger
            .icrc2_approve(
                to,
                sns_amount_e8s,
                Some(expiry_time_nsec),
                self.transaction_fee_e8s_or_panic(),
                self.sns_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making SNS Token treasury transfer: {e}"),
                )
            })?;

        self.nns_ledger
            .icrc2_approve(
                to,
                icp_amount_e8s,
                Some(expiry_time_nsec),
                icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s(),
                self.icp_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making ICP Token treasury transfer: {e}"),
                )
            })?;

        Ok(())
    }
```

**File:** rs/sns/governance/src/extensions.rs (L1566-1601)
```rust
    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;

    // 2. Call deposit on treasury manager
    let balances = governance
        .env
        .call_canister(extension_canister_id, "deposit", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.deposit failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error decoding TreasuryManager.deposit response: {err:?}"),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.deposit failed: {err:?}"),
            )
        })?;
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
