/**
 * Domain types re-exported from the generated OpenAPI schema (frontend-architecture
 * §5.1, "Types-only imports"). Features import from HERE, never from the raw
 * `schema.gen.ts` — that file is generated/eslint-disabled (IMP-5) and may be
 * regenerated at any time. This module is the stable, hand-curated surface.
 */
import type { components, paths } from './schema.gen';

/** All generated path operations — used by `createClient<ApiPaths>` in client.ts. */
export type ApiPaths = paths;

type Schemas = components['schemas'];

// --- Auth & session ---
export type TokenPairResponse = Schemas['TokenPairResponse'];
export type UserMeResponse = Schemas['UserMeResponse'];
export type MembershipSummary = Schemas['MembershipSummary'];
export type SignupRequest = Schemas['SignupRequest'];
export type SignupResponse = Schemas['SignupResponse'];
export type LoginRequest = Schemas['LoginRequest'];
export type VerifyEmailRequest = Schemas['VerifyEmailRequest'];
export type VerifyEmailResponse = Schemas['VerifyEmailResponse'];
export type EmailOnlyRequest = Schemas['EmailOnlyRequest'];
export type PasswordResetConfirmRequest = Schemas['PasswordResetConfirmRequest'];
export type DetailResponse = Schemas['DetailResponse'];

// --- Tenancy ---
export type Workspace = Schemas['Workspace'];
export type WorkspaceCreate = Schemas['WorkspaceCreate'];
export type Membership = Schemas['Membership'];
export type RoleEnum = Schemas['RoleEnum'];
export type AuditEntry = Schemas['AuditEntry'];

// --- API keys ---
export type ApiKeyListItem = Schemas['ApiKeyListItem'];
export type ApiKeyCreate = Schemas['ApiKeyCreate'];
export type ApiKeyCreated = Schemas['ApiKeyCreated'];
export type ScopesEnum = Schemas['ScopesEnum'];

// --- Scenarios & instances ---
export type ScenarioSummary = Schemas['ScenarioSummary'];
export type ScenarioDetail = Schemas['ScenarioDetail'];
export type VersionSummary = Schemas['VersionSummary'];
export type ScenarioInstance = Schemas['ScenarioInstance'];
export type InstanceCreate = Schemas['InstanceCreate'];
export type Configuration = Schemas['Configuration'];
export type ConfigurationReplace = Schemas['ConfigurationReplace'];
export type ManifestVersionDetail = Schemas['ManifestVersionDetail'];

// --- Streams & events ---
export type StreamResponse = Schemas['StreamResponse'];
export type StreamCreate = Schemas['StreamCreate'];
export type StreamStatsResponse = Schemas['StreamStatsResponse'];
export type EventsPage = Schemas['EventsPage'];

// --- Chaos & answer key (Phase 9) ---
export type ChaosPolicyResponse = Schemas['ChaosPolicyResponse'];
export type AnswerKeySummary = Schemas['AnswerKeySummary'];
export type AnswerKeyInjection = Schemas['AnswerKeyInjection'];
export type AnswerKeyInjectionsPage = Schemas['AnswerKeyInjectionsPage'];
