The key code is now visible. Let me verify the exact execution order in `get_node_providers_rewards` and the `TryFrom` implementation.

The vulnerability is real, but the question's mechanism description is **partially wrong** in an important way. Here is the precise analysis:

---

### Title
Panic (canister trap) via `None` fields in `DateUtc` in `TryFrom<DateUtc> for NaiveDate` — (`rs/node_rewards/canister/api/src/lib.rs`)

### Summary

`TryFrom<DateUtc> for NaiveDate` uses `.expect()` on `Option<u32>` fields. `.expect()` panics — it does not return `Err`. Any unprivileged caller can send a `GetNodeProvidersRewardsRequest` with `None` fields in `from_day` or `to_day` and trigger a canister trap.

### Finding Description

`DateUtc` is defined with all fields as `Option<u32>`: [1](#0-0) 

The `TryFrom<DateUtc> for NaiveDate` implementation calls `.expect()` on each field: [2](#0-1) 

`.expect()` on a `None` value **panics** — it does not return `Err`. The function signature claims `Result<Self, Self::Error>` but the body never reaches the `Err` branch when fields are `None`; it unwinds via panic, which in Wasm becomes a trap.

In `get_node_providers_rewards`, the conversion is called **before** the `validate_reward_period` guard: [3](#0-2) 

Lines 318–319 convert `DateUtc → NaiveDate` (panicking on `None`). Line 320 calls the guard. The guard is never reached. The question's framing about the `Ord` derivation and guard bypass is a **red herring** — the panic occurs before the guard is ever evaluated.

The endpoint is publicly callable with no authorization: [4](#0-3) 

### Impact Explanation

Any unprivileged caller sends `GetNodeProvidersRewardsRequest { from_day: DateUtc { year: None, month: None, day: None }, to_day: ... }`. The canister traps on that call. In IC, a trap on an update call rolls back state and returns a system-level trap error to the caller rather than a clean Candid `Err`. The canister itself survives (other calls continue), but the call fails uncleanly and the behavior is exploitable for targeted DoS on this endpoint.

### Likelihood Explanation

Trivially reachable. `DateUtc` fields are `Option<u32>` in the Candid interface, so omitting them (encoding as `null`/absent) is valid. No privilege is required.

### Recommendation

Replace `.expect()` with `ok_or(...)` in `TryFrom<DateUtc> for NaiveDate`:

```rust
fn try_from(value: DateUtc) -> Result<Self, Self::Error> {
    let year = value.year.ok_or("Year is missing")? as i32;
    let month = value.month.ok_or("Month is missing")?;
    let day = value.day.ok_or("Day is missing")?;
    NaiveDate::from_ymd_opt(year, month, day)
        .ok_or(format!("Invalid date: {:?}", value))
}
```

This makes the function return `Err` instead of panicking, and the `?` on line 318 of `mod.rs` will then propagate it cleanly as a `GetNodeProvidersRewardsResponse::Err(...)`.

### Proof of Concept

State-machine test: call `get_node_providers_rewards` with `from_day = DateUtc { year: None, month: None, day: None }` and any `to_day`. Assert the canister returns a Candid-level `Err(String)` response. Currently it traps instead.

### Citations

**File:** rs/node_rewards/canister/api/src/lib.rs (L29-36)
```rust
#[derive(
    PartialOrd, Ord, Eq, candid::CandidType, candid::Deserialize, Clone, Copy, PartialEq, Debug,
)]
pub struct DateUtc {
    pub year: Option<u32>,
    pub month: Option<u32>,
    pub day: Option<u32>,
}
```

**File:** rs/node_rewards/canister/api/src/lib.rs (L63-70)
```rust
    fn try_from(value: DateUtc) -> Result<Self, Self::Error> {
        NaiveDate::from_ymd_opt(
            value.year.expect("Year is missing") as i32,
            value.month.expect("Month is missing"),
            value.day.expect("Day is missing"),
        )
        .ok_or(format!("Invalid date: {:?}", value))
    }
```

**File:** rs/node_rewards/canister/src/canister/mod.rs (L318-320)
```rust
        let from_date = NaiveDate::try_from(request.from_day)?;
        let to_date = NaiveDate::try_from(request.to_day)?;
        canister.with_borrow(|c| c.validate_reward_period(from_date, to_date))?;
```

**File:** rs/node_rewards/canister/node-rewards-canister.did (L113-113)
```text
    get_node_providers_rewards: (GetNodeProvidersRewardsRequest) -> (GetNodeProvidersRewardsResponse);
```
