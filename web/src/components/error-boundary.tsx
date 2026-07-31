"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Minimal render error boundary. Used to keep optional/decorative UI (e.g. the
 * K5 graph visualization) fail-open: if the child throws while rendering, we
 * swap in `fallback` instead of crashing the surrounding page.
 *
 * Reset semantics: a change in `resetKey` clears a captured error so the child
 * is retried (e.g. when the user navigates to a different wiki slug).
 */
export class ErrorBoundary extends Component<
  {
    children: ReactNode;
    fallback: ReactNode;
    resetKey?: unknown;
    onError?: (error: Error, info: ErrorInfo) => void;
  },
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidUpdate(prevProps: { resetKey?: unknown }) {
    if (this.state.hasError && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ hasError: false });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.props.onError?.(error, info);
  }

  render() {
    return this.state.hasError ? this.props.fallback : this.props.children;
  }
}
