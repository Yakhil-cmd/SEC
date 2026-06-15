The vulnerability is confirmed present in Sei chain. Here is the full report:

---

### Title
Front-Running `CreateVestingAccount` to Permanently Block Vesting Account Creation - (`sei-cosmos/x/auth/vesting/msg_server.go`)

### Summary
Any unprivileged attacker can permanently prevent a specific address from ever becoming a vesting account by front-running a `CreateVestingAccount` transaction with a `Bank.Send` to the same recipient. The root cause is that `CreateVestingAccount` hard-rejects any recipient whose account already exists, while `Bank.SendCoins` unconditionally creates a plain `BaseAccount` for any new recipient.

### Finding Description

`CreateVestingAccount` in Sei's `sei-cosmos` fork contains an identical guard to the SEDA report:

```go
if acc := ak.GetAccount(ctx, to); acc != nil {
    return nil, sdkerrors.Wrapf(sdkerrors.ErrInvalidRequest, "account %s already exists", msg.ToAddress)
}
``` [1](#0-0) 

`Bank.SendCoins` in the same codebase creates a new `BaseAccount` for any recipient that does not yet exist:

```go
accExists := k.ak.HasAccount(ctx, toAddr)
if !accExists {
    defer telemetry.IncrCounter(1, "new", "account")
    k.ak.SetAccount(ctx, k.ak.NewAccountWithAddress(ctx, toAddr))
}
``` [2](#0-1) 

The same account-creation side-effect also exists in `InputOutputCoins` (multi-send): [3](#0-2) 

**Attack path:**
1. Victim submits `MsgCreateVestingAccount` targeting address `B`.
2. Attacker observes the pending transaction in the mempool and front-runs it with `MsgSend` (or `MsgMultiSend`) sending any non-zero amount to address `B`.
3. `Bank.SendCoins` creates a plain `BaseAccount` for `B`.
4. Victim's `CreateVestingAccount` executes and hits the `acc != nil` guard, returning `ErrInvalidRequest`.
5. Because accounts are never deleted on-chain, address `B` is permanently ineligible to become a vesting account.

### Impact Explanation
The intended recipient is permanently blocked from receiving vested funds via `CreateVestingAccount`. The sender's funds are not lost (the transaction simply fails), but the vesting schedule for that address can never be established. This is a permanent, irreversible griefing of a specific address's vesting eligibility. Under the Sei bounty scope this maps most closely to **Low** — it does not cause fund loss/freeze, does not halt validators, and does not crash RPC nodes, but it does cause wrong/broken behavior in a core chain feature (vesting) exploitable by any unprivileged actor.

### Likelihood Explanation
The attack requires only a small amount of any sendable token and knowledge of the target address (observable from the mempool). No privileged access, validator key, or governance power is needed. The cost to the attacker is negligible (a single `Bank.Send` of 1 usei). The damage is permanent.

### Recommendation
Remove the hard rejection of pre-existing accounts. Instead, check whether the existing account is already a vesting account (reject that case) and otherwise upgrade the existing `BaseAccount` to the requested vesting account type in-place, preserving its sequence number and public key. This is the same mitigation suggested in the SEDA report.

### Proof of Concept
1. Alice submits `MsgCreateVestingAccount{from: alice, to: bob, amount: 1000usei, end_time: T}`.
2. Attacker submits `MsgSend{from: attacker, to: bob, amount: 1usei}` with higher gas price, landing first in the block.
3. `Bank.SendCoins` creates a plain `BaseAccount` for `bob`.
4. Alice's `CreateVestingAccount` reaches line 54 of `msg_server.go`, finds `bob`'s account non-nil, and returns `ErrInvalidRequest: account bob already exists`.
5. Alice retries with a different recipient address — the attacker repeats step 2 indefinitely at negligible cost. [4](#0-3) [5](#0-4)

### Citations

**File:** sei-cosmos/x/auth/vesting/msg_server.go (L32-56)
```go
func (s msgServer) CreateVestingAccount(goCtx context.Context, msg *types.MsgCreateVestingAccount) (*types.MsgCreateVestingAccountResponse, error) {
	ctx := sdk.UnwrapSDKContext(goCtx)
	ak := s.AccountKeeper
	bk := s.BankKeeper

	if err := bk.IsSendEnabledCoins(ctx, msg.Amount...); err != nil {
		return nil, err
	}

	from, err := sdk.AccAddressFromBech32(msg.FromAddress)
	if err != nil {
		return nil, err
	}
	to, err := sdk.AccAddressFromBech32(msg.ToAddress)
	if err != nil {
		return nil, err
	}

	if bk.BlockedAddr(to) {
		return nil, sdkerrors.Wrapf(sdkerrors.ErrUnauthorized, "%s is not allowed to receive funds", msg.ToAddress)
	}

	if acc := ak.GetAccount(ctx, to); acc != nil {
		return nil, sdkerrors.Wrapf(sdkerrors.ErrInvalidRequest, "account %s already exists", msg.ToAddress)
	}
```

**File:** sei-cosmos/x/bank/keeper/send.go (L146-150)
```go
		accExists := k.ak.HasAccount(ctx, outAddress)
		if !accExists {
			defer telemetry.IncrCounter(1, "new", "account")
			k.ak.SetAccount(ctx, k.ak.NewAccountWithAddress(ctx, outAddress))
		}
```

**File:** sei-cosmos/x/bank/keeper/send.go (L158-173)
```go
func (k BaseSendKeeper) SendCoins(ctx sdk.Context, fromAddr sdk.AccAddress, toAddr sdk.AccAddress, amt sdk.Coins) error {
	if err := k.SendCoinsWithoutAccCreation(ctx, fromAddr, toAddr, amt); err != nil {
		return err
	}

	// Create account if recipient does not exist.
	//
	// NOTE: This should ultimately be removed in favor a more flexible approach
	// such as delegated fee messages.
	accExists := k.ak.HasAccount(ctx, toAddr)
	if !accExists {
		defer telemetry.IncrCounter(1, "new", "account")
		k.ak.SetAccount(ctx, k.ak.NewAccountWithAddress(ctx, toAddr))
	}

	return nil
```
