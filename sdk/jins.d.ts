export class JinsError extends Error {
  status: number; code: string; requestId?: string | null; details: Record<string, unknown>;
}
export interface JinsOptions { timeout?: number; retries?: number; fetch?: typeof fetch }
export interface Meta { limit?: number; offset?: number; has_more?: boolean }
export type Verdict = 'trusted' | 'neutral' | 'risk' | 'scam' | 'unknown';
export interface CheckResult { found: boolean; id: number | null; username?: string | null; verdict: Verdict; stars?: number; score?: number; status?: string; label?: string; tier?: string }
export interface WebhookEvent<T = any> { id: string; type: string; created_at: string; data: T }
export class Jins {
  constructor(baseUrl: string, apiKey: string, opts?: JinsOptions);
  lastMeta: Meta | null;
  lastRateLimit: { limit: number; remaining: number; reset: number } | null;
  request<T = any>(method: string, path: string, o?: { params?: Record<string, any>; body?: any; idempotencyKey?: string }): Promise<T>;
  get<T = any>(path: string, params?: Record<string, any>): Promise<T>;
  ping(): Promise<{ pong: boolean; time: string; version: string }>;
  me(): Promise<any>; stats(): Promise<any>;
  check(ref: string | number): Promise<CheckResult>;
  user(ref: string | number): Promise<any>;
  reviews(ref: string | number, limit?: number, offset?: number): Promise<any>;
  baseSearch(q: string): Promise<any>; baseList(kind?: 'top' | 'minus'): Promise<any>; baseProfile(ref: string | number): Promise<any>;
  topics(): Promise<any[]>;
  posts(o?: { topic?: number; q?: string; projectId?: number; authorId?: number; limit?: number; offset?: number }): Promise<any[]>;
  iterPosts(o?: { pageSize?: number; topic?: number; q?: string; projectId?: number; authorId?: number }): AsyncGenerator<any>;
  post(id: number): Promise<any>; submitPost(text: string, topicId: number, projectId?: number): Promise<any>;
  projects(filters?: Record<string, any>): Promise<any[]>; project(id: number): Promise<any>;
  deals(o?: { kind?: 'all' | 'escrow' | 'direct'; status?: string; limit?: number; offset?: number }): Promise<any[]>;
  deal(kind: 'escrow' | 'direct', id: number): Promise<any>;
  createEscrowDeal(d: { sellerId: number; amount: number; terms: string; postId?: number }, idempotencyKey?: string): Promise<any>;
  createDirectDeal(d: { sellerId: number; amount: number; terms: string; postId?: number }, idempotencyKey?: string): Promise<any>;
  dealMessage(kind: 'escrow' | 'direct', id: number, body: string): Promise<any>;
  dealAction(kind: 'escrow' | 'direct', id: number, action: string, reason?: string): Promise<any>;
  receipts(): Promise<any[]>; receiptPdf(id: number): Promise<Uint8Array>;
  chats(): Promise<any>; messages(peer: number, postId?: number): Promise<any>;
  sendMessage(peer: number, text: string, o?: { postId?: number; replyTo?: number }): Promise<any>;
  webhooks(): Promise<any[]>; createWebhook(url: string, events?: string[], description?: string): Promise<any & { secret: string }>;
  testWebhook(id: number): Promise<any>; deleteWebhook(id: number): Promise<any>;
}
export function verifyWebhook(secret: string, signatureHeader: string, rawBody: string | Buffer, toleranceSec?: number): boolean;
