import { CvizEvent, CvizEventType } from "../types/cviz";

type Listener = (event: CvizEvent) => void;

/**
 * Lightweight pub/sub event bus for CViz extension events.
 * All UI components subscribe here instead of managing their own polling.
 */
export class CvizEventBus {
  private readonly listeners = new Map<CvizEventType, Set<Listener>>();

  public on(type: CvizEventType, listener: Listener): () => void {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, new Set());
    }
    this.listeners.get(type)!.add(listener);
    return () => this.off(type, listener);
  }

  public off(type: CvizEventType, listener: Listener): void {
    this.listeners.get(type)?.delete(listener);
  }

  public emit(type: CvizEventType, payload?: unknown): void {
    const event: CvizEvent = { type, payload };
    for (const listener of this.listeners.get(type) ?? []) {
      try {
        listener(event);
      } catch {
        // individual listener failures must not break the bus
      }
    }
  }

  public dispose(): void {
    this.listeners.clear();
  }
}

/** Singleton event bus shared across the extension. */
export const cvizEvents = new CvizEventBus();
