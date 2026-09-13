import type { ClientChannel } from "ssh2";
import { ReadBuffer, serializeMessage } from "@modelcontextprotocol/sdk/shared/stdio.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";

export interface SshMcpTransportOptions {
  maxInputBytes: number;
  maxOutputBytes: number;
}

export class SshMcpTransport implements Transport {
  onclose?: () => void;
  onerror?: (error: Error) => void;
  onmessage?: <T extends JSONRPCMessage>(message: T) => void;

  private readonly readBuffer: ReadBuffer;
  private channel?: ClientChannel;
  private outputBytes = 0;
  private closed = false;
  private stderrChunks: Buffer[] = [];
  private stderrBytes = 0;

  constructor(
    private readonly open: () => Promise<ClientChannel>,
    private readonly options: SshMcpTransportOptions,
  ) {
    this.readBuffer = new ReadBuffer({ maxBufferSize: options.maxOutputBytes });
  }

  get stderr(): string {
    return Buffer.concat(this.stderrChunks).toString("utf8");
  }

  async start(): Promise<void> {
    if (this.channel) throw new Error("SSH MCP transport is already started");
    const channel = await this.open();
    this.channel = channel;
    channel.on("data", (value: Buffer | string) => this.receive(Buffer.isBuffer(value) ? value : Buffer.from(value)));
    channel.stderr.on("data", (value: Buffer | string) => {
      const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
      this.outputBytes += chunk.length;
      const room = Math.max(0, this.options.maxOutputBytes - this.stderrBytes);
      if (room > 0) {
        this.stderrChunks.push(chunk.length > room ? chunk.subarray(0, room) : chunk);
        this.stderrBytes += Math.min(chunk.length, room);
      }
      this.enforceOutputLimit();
    });
    channel.on("error", (error: Error) => this.fail(error));
    channel.once("close", () => this.finish());
  }

  async send(message: JSONRPCMessage): Promise<void> {
    const channel = this.channel;
    if (!channel || this.closed) throw new Error("SSH MCP transport is not connected");
    const serialized = serializeMessage(message);
    if (Buffer.byteLength(serialized, "utf8") > this.options.maxInputBytes) {
      throw new Error(`MCP request exceeds ${this.options.maxInputBytes} bytes`);
    }
    await new Promise<void>((resolve, reject) => {
      channel.write(serialized, (error?: Error | null) => (error ? reject(error) : resolve()));
    });
  }

  async close(): Promise<void> {
    const channel = this.channel;
    if (!channel || this.closed) {
      this.finish();
      return;
    }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        channel.close();
        resolve();
      }, 1_000);
      timer.unref();
      channel.once("close", () => {
        clearTimeout(timer);
        resolve();
      });
      channel.end();
    });
  }

  private receive(chunk: Buffer): void {
    this.outputBytes += chunk.length;
    if (!this.enforceOutputLimit()) return;
    try {
      this.readBuffer.append(chunk);
      for (;;) {
        const message = this.readBuffer.readMessage();
        if (message === null) break;
        this.onmessage?.(message);
      }
    } catch (err) {
      this.fail(err as Error);
    }
  }

  private enforceOutputLimit(): boolean {
    if (this.outputBytes <= this.options.maxOutputBytes) return true;
    this.fail(new Error(`MCP process output exceeds ${this.options.maxOutputBytes} bytes`));
    this.channel?.close();
    return false;
  }

  private fail(error: Error): void {
    this.onerror?.(error);
  }

  private finish(): void {
    if (this.closed) return;
    this.closed = true;
    this.channel = undefined;
    this.readBuffer.clear();
    this.onclose?.();
  }
}