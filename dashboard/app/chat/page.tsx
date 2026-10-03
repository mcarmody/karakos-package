"use client";

/**
 * /chat — the chat page, full width. A wrapper around the shared
 * ChatSurface: `variant="page"` is the full-page behaviour (agent picker,
 * TabShelf clearance), and AppShell keys its non-scrolling-`main` branch off
 * the /chat pathname.
 */

import ChatSurface from "@/app/components/ChatSurface";

export default function ChatPage() {
  return <ChatSurface variant="page" />;
}
