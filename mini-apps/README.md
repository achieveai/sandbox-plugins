# mini-apps

Skills for building AgentWorkBench (AWB) Mini Web Apps that work with the chat.

## Skills

- **mini-app-sdk**: wires the AWB Mini App SDK (`/_awb/sdk.js`, SDK version 1) into an app.
  The user can point at any part of the app and ask about it. The agent reads what the app
  shows (`ReadMiniAppContext`) and drives it through declared commands (`RunMiniAppCommand`).
  Example: `skills/mini-app-sdk/reference/deck-app.py`.

## Versioning

The skill names the SDK version it was written for (`metadata.sdk-version` in `SKILL.md`).
When AWB ships a new SDK version, update the skill and bump this plugin's version.

The SDK itself lives in AWB: `src/Server/Web/AgentWorkBench.Web/SampleApi/MiniWebApps/awb-sdk.js`.
