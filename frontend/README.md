# AICC Builder Frontend

<div align="center">

**Real-Time Multi-Agent Chat Interface**

[![React](https://img.shields.io/badge/React-18.3-blue?style=flat&logo=react)](https://reactjs.org/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.7-blue?style=flat&logo=typescript)](https://typescriptlang.org/)
[![Vite](https://img.shields.io/badge/Vite-6.0-purple?style=flat&logo=vite)](https://vitejs.dev/)
[![Tailwind CSS](https://img.shields.io/badge/Tailwind-3.4-cyan?style=flat&logo=tailwindcss)](https://tailwindcss.com/)

</div>

---

## Overview

A responsive single-page application that provides a real-time chat interface for interacting with the AICC Builder multi-agent system. Features include sub-agent activity visualization, asset preview streaming, file attachments, session management, and auto-save.

> 📖 WebSocket protocol details: [docs/architecture.md](../docs/architecture.md#websocket-protocol)

---

## Directory Structure

```
frontend/src/
├── components/              # 14 UI components
│   ├── ChatWindow.tsx       # Main chat interface with message list + input
│   ├── MessageBubble.tsx    # Message rendering (Markdown, syntax highlight)
│   ├── SubagentBubble.tsx   # Sub-agent activity visualization
│   ├── AssetPreviewBubble.tsx # Generated asset preview (code, YAML, JSON)
│   ├── ProgressSidebar.tsx  # Generation progress tracking
│   ├── SessionSidebar.tsx   # Session list and management
│   ├── ChatAttachmentButton.tsx # File attachment upload
│   ├── AttachmentPreview.tsx # Attached file preview
│   ├── ChatEmptyState.tsx   # Empty chat welcome screen
│   ├── ContactFlowPreview.tsx # Contact Flow visual preview
│   ├── MermaidDiagram.tsx   # Mermaid.js diagram renderer
│   ├── VirtualizedItem.tsx  # Virtualized list item wrapper
│   ├── Header.tsx           # App header with user info
│   └── TypingIndicator.tsx  # Agent typing animation
├── hooks/
│   ├── useWebSocket.ts      # WebSocket connection management (94KB)
│   └── useAutoSave.ts       # Automatic session state persistence
├── stores/                  # Zustand state management
│   ├── authStore.ts         # Cognito authentication state
│   ├── builderStore.ts      # Messages, progress, assets, streaming
│   └── sessionStore.ts      # Session list, active session, CRUD
├── services/
│   ├── auth.ts              # Cognito authentication service
│   └── sessions.ts          # Session REST API client
├── types/
│   └── index.ts             # TypeScript types (WebSocket messages, assets, etc.)
├── lib/
│   ├── utils.ts             # Helper functions
│   └── zipUtils.ts          # ZIP file handling for asset downloads
├── pages/
│   └── LoginPage.tsx        # Cognito login with new-password flow
├── App.tsx                  # Root component with routing
├── main.tsx                 # Entry point
└── index.css                # Tailwind + custom styles
```

---

## Key Components

### Chat Interface

| Component | Description |
|-----------|-------------|
| `ChatWindow` | Main chat with message list, input, attachment support |
| `MessageBubble` | Renders messages with Markdown, syntax highlighting, copy button |
| `SubagentBubble` | Shows sub-agent activity: name, status, internal tool calls, streaming text |
| `AssetPreviewBubble` | Displays generated assets with syntax highlighting and download |
| `TypingIndicator` | Animated dots during agent processing |
| `ChatEmptyState` | Welcome screen with quick-start suggestions |

### Sidebars

| Component | Description |
|-----------|-------------|
| `ProgressSidebar` | Real-time generation progress with sub-steps per asset type |
| `SessionSidebar` | Session list with create/restore/delete, auto-save indicator |

### Specialized Renderers

| Component | Description |
|-----------|-------------|
| `ContactFlowPreview` | Visual Contact Flow block diagram |
| `MermaidDiagram` | Renders Mermaid.js diagrams from generated content |
| `AttachmentPreview` | Image/document preview for file attachments |
| `VirtualizedItem` | Performance wrapper for long message lists |

---

## State Management

### `authStore` — Authentication

Manages Cognito authentication: login, logout, token refresh, new-password challenge.

### `builderStore` — Application State

Core application state including:
- `messages[]` — Chat messages (user, assistant, tool, asset, subagent)
- `progress` — Generation progress items with sub-steps
- `isStreaming` — Whether agent is currently streaming
- `assetPreviews` — Generated asset content for preview
- WebSocket message handlers for all 30+ message types

### `sessionStore` — Session Management

- `sessions[]` — List of all sessions
- `activeSessionId` — Currently active session
- CRUD operations via REST API (`services/sessions.ts`)
- Auto-save integration with `useAutoSave` hook

---

## Hooks

### `useWebSocket`

Manages the WebSocket connection to AgentCore Runtime:
- Cognito-authenticated connection (SigV4 signing)
- Automatic reconnection with exponential backoff
- Message routing to appropriate store handlers
- Delta streaming accumulation for large assets
- Heartbeat/ping keepalive

### `useAutoSave`

Automatically persists session state:
- Debounced save on message changes
- Saves to backend via sessions API
- Visual indicator in SessionSidebar

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `VITE_AGENT_RUNTIME_ARN` | AgentCore Runtime ARN |
| `VITE_WS_URL` | WebSocket URL (derived from ARN) |
| `VITE_USER_POOL_ID` | Cognito User Pool ID |
| `VITE_USER_POOL_CLIENT_ID` | Cognito Client ID |
| `VITE_IDENTITY_POOL_ID` | Cognito Identity Pool ID |
| `VITE_COGNITO_REGION` | Cognito region |
| `VITE_SESSION_API_URL` | Session REST API URL |

These are auto-generated by `deploy.sh` into `.env`.

---

## Development

```bash
npm install
npm run dev      # http://localhost:5173
npm run build    # Production build → dist/
npm run lint     # ESLint
npm run preview  # Preview production build
```

---

## Tech Stack

| Technology | Version | Purpose |
|------------|---------|---------|
| React | 18.3 | UI framework |
| TypeScript | 5.7 | Type safety |
| Vite | 6.0 | Build tool |
| Tailwind CSS | 3.4 | Styling |
| Zustand | 5.0 | State management |
| React Router | 6.28 | Navigation |
| Radix UI | 1.x | Accessible primitives |
| Lucide React | 0.460 | Icons |
| React Markdown | 9.0 | Message rendering |
| Mermaid.js | Latest | Diagram rendering |
| amazon-cognito-identity-js | 6.3 | Authentication |

---

<div align="center">

**Built with React + TypeScript + Vite**

</div>
