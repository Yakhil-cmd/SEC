### Title
SNS Voting Rewards Permanently Lost When Proposals Settled With Zero Voting Power — (File: rs/sns/governance/src/governance.rs)

### Summary
In the SNS governance `distribute_rewards` function, when proposals reach `ReadyToSettle` during a reward round where `total_reward_shares == dec!(0)` (no neuron cast an eligible vote), the entire `rewards_purse_e8s` — including any accumulated rollover from prior empty rounds — is permanently destroyed rather than carried forward. The proposals are irrevocably settled, so the rollover gate never re-opens for those tokens.

### Finding Description
`distribute_rewards` builds `rewards_purse_e8s` in two parts:

```rust
let rewards_purse_e8s = {
    let mut result = Decimal::from(
        self.latest_reward_event()
            .e8s_equivalent_to_be_rolled_over(), // ← prior rollover
    );
    let supply = i2d(supply.get_e8s());
    for i in 1..=new_rounds_count {
        ...
        result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
    }
    result
};
``` [1](#0-0) 

When `total_reward_shares == dec!(0)`, maturity distribution is skipped with only a log warning:

```rust
if total_reward_shares == dec!(0) {
    log!(ERROR, "Warning: total_reward_shares is 0. ...");
} else {
    // distribute maturity
}
```

<cite repo="Jaredbentat/ic--014" path

### Citations

**File:** rs/sns/governance/src/governance.rs (L5854-5875)
```rust
        let rewards_purse_e8s = {
            let mut result = Decimal::from(
                self.latest_reward_event()
                    .e8s_equivalent_to_be_rolled_over(),
            );
            let supply = i2d(supply.get_e8s());

            for i in 1..=new_rounds_count {
                let seconds_since_genesis = round_duration_seconds
                    .saturating_mul(i)
                    .saturating_add(reward_start_timestamp_seconds)
                    .saturating_sub(self.proto.genesis_timestamp_seconds);

                let current_reward_rate = voting_rewards_parameters.reward_rate_at(
                    crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
                );

                result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
            }

            result
        };
```
