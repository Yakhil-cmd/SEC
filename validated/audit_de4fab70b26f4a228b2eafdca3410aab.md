### Title
`TimeoutPacket()` Incorrectly Enforces OPEN Channel State, Permanently Locking Optimistically-Sent Funds - (File: `sei-ibc-go/modules/core/04-channel/keeper/timeout.go`)

---

### Summary

`SendPacket` permits sending packets on channels in any non-`CLOSED` state (`INIT`, `TRYOPEN`, `OPEN`), enabling optimistic packet sends. However, `TimeoutPacket` enforces that the channel must be in `OPEN` state before processing a timeout. If a channel handshake stalls in `INIT` or `TRYOPEN` without the counterparty channel being `CLOSED`, neither `TimeoutPacket` nor `TimeoutOnClose` can be invoked, permanently locking any escrowed ICS-20 tokens.

---

### Finding Description

**`SendPacket`** in `sei-ibc-go/modules/core/04-channel/keeper/packet.go` only rejects sends when the channel is `CLOSED`:

```go
if channel.State == types.CLOSED {
    return sdkerrors.Wrapf(...)
}
``` [1](#0-0) 

This explicitly allows packets to be sent on channels in `INIT` or `TRYOPEN` state — the "optimistic packet send" design.

**`TimeoutPacket`** in `sei-ibc-go/modules/core/04-channel/keeper/timeout.go` then enforces the opposite — it requires `OPEN`:

```go
if channel.State != types.OPEN {
    return sdkerrors.Wrapf(
        types.ErrInvalidChannelState,
        "channel state is not OPEN (got %s)", channel.State.String(),
    )
}
``` [2](#0-1) 

**`TimeoutOnClose`** does not have this OPEN check, but it requires proving the **counterparty** channel is `CLOSED`: [3](#0-2) 

If the counterparty channel is stuck in `INIT` or `TRYOPEN` (not `CLOSED`), `TimeoutOnClose` also cannot be used.

**ICS-20 `sendTransfer`** does not independently check channel state before calling `SendPacket`: [4](#0-3) 

The channel states are defined as `INIT=1`, `TRYOPEN=2`, `OPEN=3`, `CLOSED=4`: [5](#0-4) 

---

### Impact Explanation

When a packet is sent optimistically on a channel in `INIT` or `TRYOPEN` state, and the channel handshake stalls (never reaching `OPEN`, and the counterparty channel never reaching `CLOSED`):

1. `TimeoutPacket` returns `ErrInvalidChannelState` — blocked by the `!= OPEN` check.
2. `TimeoutOnClose` cannot be used — the counterparty channel is not `CLOSED`.
3. The packet commitment remains permanently in state.
4. `OnTimeoutPacket` is never called on the ICS-20 module.
5. Escrowed tokens (for native sends) or burned vouchers (for IBC denom sends) are permanently unrecoverable.

This is a **fund freeze** scenario. The amount locked depends on what was sent; for ICS-20 transfers this can easily exceed $5k, qualifying as **Critical** under the Sei bounty scope.

---

### Likelihood Explanation

The scenario requires:
1. A channel in `INIT` or `TRYOPEN` state with a valid capability (created via `ChanOpenInit`/`ChanOpenTry`).
2. A user or application sending a packet optimistically on that channel (permitted by `SendPacket`).
3. The channel handshake stalling — the counterparty chain goes offline, the relayer stops, or the handshake is deliberately abandoned — without the counterparty channel being explicitly `CLOSED`.

This is a realistic production scenario. Relayer failures and incomplete handshakes are common. An adversary could also deliberately initiate a `ChanOpenInit`, wait for a victim to send tokens optimistically, then abandon the handshake.

---

### Recommendation

Remove the `channel.State != types.OPEN` check from `TimeoutPacket` in `sei-ibc-go/modules/core/04-channel/keeper/timeout.go` (lines 86–91). The existence of a valid packet commitment (already checked at line 75) is sufficient proof that the packet was legitimately sent. The timeout height/timestamp proof already validates the timeout condition independently of channel state.

---

### Proof of Concept

1. Chain A calls `ChanOpenInit` → channel A is in `INIT` state.
2. Chain B calls `ChanOpenTry` → channel B is in `TRYOPEN` state.
3. User calls `MsgTransfer` on chain A, sending 10,000 USDC. `sendTransfer` calls `SendPacket`. `SendPacket` checks `channel.State == CLOSED` → false, so the packet is committed. Tokens are escrowed.
4. The relayer stops. Chain A's channel stays in `INIT`. Chain B's channel stays in `TRYOPEN`. Neither is `CLOSED`.
5. The packet's `timeoutHeight` passes on chain B.
6. Relayer (or user) submits `MsgTimeout` on chain A. `TimeoutPacket` is called. At line 86: `channel.State (INIT) != OPEN` → returns `ErrInvalidChannelState`. Timeout fails.
7. Relayer submits `MsgTimeoutOnClose` on chain A. `TimeoutOnClose` tries to verify that chain B's channel is `CLOSED`. It is `TRYOPEN`, not `CLOSED` → `VerifyChannelState` fails.
8. No timeout path succeeds. The 10,000 USDC remain permanently escrowed on chain A with no recovery mechanism.

### Citations

**File:** sei-ibc-go/modules/core/04-channel/keeper/packet.go (L46-51)
```go
	if channel.State == types.CLOSED {
		return sdkerrors.Wrapf(
			types.ErrInvalidChannelState,
			"channel is CLOSED (got %s)", channel.State.String(),
		)
	}
```

**File:** sei-ibc-go/modules/core/04-channel/keeper/timeout.go (L86-91)
```go
	if channel.State != types.OPEN {
		return sdkerrors.Wrapf(
			types.ErrInvalidChannelState,
			"channel state is not OPEN (got %s)", channel.State.String(),
		)
	}
```

**File:** sei-ibc-go/modules/core/04-channel/keeper/timeout.go (L183-256)
```go
func (k Keeper) TimeoutOnClose(
	ctx sdk.Context,
	chanCap *capabilitytypes.Capability,
	packet exported.PacketI,
	proof,
	proofClosed []byte,
	proofHeight exported.Height,
	nextSequenceRecv uint64,
) error {
	channel, found := k.GetChannel(ctx, packet.GetSourcePort(), packet.GetSourceChannel())
	if !found {
		return sdkerrors.Wrapf(types.ErrChannelNotFound, "port ID (%s) channel ID (%s)", packet.GetSourcePort(), packet.GetSourceChannel())
	}

	capName := host.ChannelCapabilityPath(packet.GetSourcePort(), packet.GetSourceChannel())
	if !k.scopedKeeper.AuthenticateCapability(ctx, chanCap, capName) {
		return sdkerrors.Wrapf(
			types.ErrInvalidChannelCapability,
			"channel capability failed authentication with capability name %s", capName,
		)
	}

	if packet.GetDestPort() != channel.Counterparty.PortId {
		return sdkerrors.Wrapf(
			types.ErrInvalidPacket,
			"packet destination port doesn't match the counterparty's port (%s ≠ %s)", packet.GetDestPort(), channel.Counterparty.PortId,
		)
	}

	if packet.GetDestChannel() != channel.Counterparty.ChannelId {
		return sdkerrors.Wrapf(
			types.ErrInvalidPacket,
			"packet destination channel doesn't match the counterparty's channel (%s ≠ %s)", packet.GetDestChannel(), channel.Counterparty.ChannelId,
		)
	}

	connectionEnd, found := k.connectionKeeper.GetConnection(ctx, channel.ConnectionHops[0])
	if !found {
		return sdkerrors.Wrap(connectiontypes.ErrConnectionNotFound, channel.ConnectionHops[0])
	}

	commitment := k.GetPacketCommitment(ctx, packet.GetSourcePort(), packet.GetSourceChannel(), packet.GetSequence())

	if len(commitment) == 0 {
		EmitTimeoutPacketEvent(ctx, packet, channel)
		// This error indicates that the timeout has already been relayed
		// or there is a misconfigured relayer attempting to prove a timeout
		// for a packet never sent. Core IBC will treat this error as a no-op in order to
		// prevent an entire relay transaction from failing and consuming unnecessary fees.
		return types.ErrNoOpMsg
	}

	packetCommitment := types.CommitPacket(k.cdc, packet)

	// verify we sent the packet and haven't cleared it out yet
	if !bytes.Equal(commitment, packetCommitment) {
		return sdkerrors.Wrapf(types.ErrInvalidPacket, "packet commitment bytes are not equal: got (%v), expected (%v)", commitment, packetCommitment)
	}

	counterpartyHops := []string{connectionEnd.GetCounterparty().GetConnectionID()}

	counterparty := types.NewCounterparty(packet.GetSourcePort(), packet.GetSourceChannel())
	expectedChannel := types.NewChannel(
		types.CLOSED, channel.Ordering, counterparty, counterpartyHops, channel.Version,
	)

	// check that the opposing channel end has closed
	if err := k.connectionKeeper.VerifyChannelState(
		ctx, connectionEnd, proofHeight, proofClosed,
		channel.Counterparty.PortId, channel.Counterparty.ChannelId,
		expectedChannel,
	); err != nil {
		return err
	}
```

**File:** sei-ibc-go/modules/apps/transfer/keeper/relay.go (L201-203)
```go
	if err := k.ics4Wrapper.SendPacket(ctx, channelCap, packet); err != nil {
		return 0, err
	}
```

**File:** sei-ibc-go/modules/core/04-channel/types/channel.pb.go (L31-44)
```go
const (
	// Default State
	UNINITIALIZED State = 0
	// A channel has just started the opening handshake.
	INIT State = 1
	// A channel has acknowledged the handshake step on the counterparty chain.
	TRYOPEN State = 2
	// A channel has completed the handshake. Open channels are
	// ready to send and receive packets.
	OPEN State = 3
	// A channel has been closed and can no longer be used to send or receive
	// packets.
	CLOSED State = 4
)
```
