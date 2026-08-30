---
description: Veloce API reference - application.
---

# Application

The application object and its configuration.

::: veloce.Veloce
::: veloce.Config
::: veloce.Plugin
::: veloce.HealthPlugin

## Signals

The pub/sub primitives and the eight signals Veloce fires around the request
and app-context lifecycle. Connect a receiver with
`request_started.connect(fn)`; see the
[Signals guide](../guide/signals.md) for the payload each one carries.

::: veloce.Signal
::: veloce.SignalResult
::: veloce.Namespace
::: veloce.request_started
::: veloce.request_finished
::: veloce.request_tearing_down
::: veloce.got_request_exception
::: veloce.message_flashed
::: veloce.appcontext_pushed
::: veloce.appcontext_popped
::: veloce.appcontext_tearing_down
