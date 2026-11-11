# House Translator API

The translator exposes a localhost API on port `8765` by default. The API is
defined entirely in logical House keys—`A B C D 1 2 3 4 L R`—so clients do not
need to know whether input came from QWERTY, another keyboard layout, or a
custom board adapter.

## Endpoints

- `GET http://127.0.0.1:8765/v1/layout` returns the current rooms, logical
  finger and thumb layers, chord outputs, and corpus counts.
- `GET http://127.0.0.1:8765/v1/state` returns a one-time state snapshot for
  startup or recovery. It requires an approved connection token.
- `WS ws://127.0.0.1:8765/v1/events` is the live pub/sub event stream.
- `POST http://127.0.0.1:8765/v1/input` feeds a logical device event into the
  translator. It requires an approved connection token.

There is deliberately no event-polling endpoint. A new WebSocket receives only
`registration_required`. It must reply with a name:

```json
{"type":"register","name":"House Trainer"}
```

Windows then displays an Allow/Deny confirmation. No state or keystroke data is
released before explicit approval. An approved subscriber receives a
`subscribed` message containing an atomic state snapshot and a random session
token, followed by pushed events as they happen. Approval lasts only while that
WebSocket is connected; reconnecting requires approval again.

## Events

Every pushed event has a monotonically increasing `id`, a `type`, and a Unix
`timestamp`. Event types are:

- `key`: logical key up/down, current pressed state, all held keys, source
  device name, and whether this changed the combined logical state.
- `stroke_started`: keys, press order, and thumbs at the start of a stroke.
- `output`: text queued for system output.
- `stroke_finished`: resolved room, keys, press order, thumbs, canonical and
  emitted output, chord/special status, corpus count, and keys still held.
- `mode_changed`, `visualizer_changed`, `ready`, and `stopping`: translator
  lifecycle and UI state.
- `overflow`: a slow subscriber lost events and should refresh `/v1/state`.

Example browser or JavaScript client:

```js
const events = new WebSocket("ws://127.0.0.1:8765/v1/events");
events.onmessage = ({ data }) => {
  const event = JSON.parse(data);
  if (event.type === "registration_required") {
    events.send(JSON.stringify({ type: "register", name: "House Trainer" }));
    return;
  }
  console.log(event.type, event);
};
```

## Logical input adapters

An adapter translates its hardware or serial protocol once, then posts logical
events. The training app only observes the invariant WebSocket stream.

```json
POST /v1/input
Authorization: Bearer TOKEN_FROM_SUBSCRIBED_MESSAGE

{"key":"A","action":"down"}
```

`key` must be one of `A B C D 1 2 3 4 L R`; `action` is `down` or `up`.
Multiple approved connections may hold the same logical key simultaneously. The key remains
pressed until every device source releases it, preventing one adapter from
prematurely ending another adapter's stroke.

Active connections appear under **API Connections** in the tray menu. Clicking
a connection immediately closes it, invalidates its token, and clears any
logical keys it was holding without emitting a partial chord.

Use `--api-port PORT` to select another port, or `--no-api` to disable the API.
The server binds only to `127.0.0.1`; browser requests are accepted only from
`localhost` or loopback HTTP(S) origins. `Origin: null` is rejected because it
can also be produced by sandboxed hostile pages. Native clients that do not
send an `Origin` header are supported.
