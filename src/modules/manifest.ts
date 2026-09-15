import path from "node:path";
import { z } from "zod";

const MODULE_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const SEMVER = /^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
const COMMAND = /^[A-Za-z0-9][A-Za-z0-9+._-]*$/;
const APT_PACKAGE = /^[a-z0-9][a-z0-9+._-]*$/;
const INSTALL_OPTION = /^[a-z][A-Za-z0-9]*$/;

const RelativePathSchema = z.string().min(1).max(200).refine((value) => {
  if (value.includes("\0") || value.includes("\\") || path.posix.isAbsolute(value)) return false;
  const normalized = path.posix.normalize(value);
  return normalized === value && normalized !== "." && !normalized.startsWith("../");
}, "must be a normalized relative POSIX path without traversal");

const AcceleratorRequirementSchema = z
  .object({
    kind: z.string().min(1).max(50),
    vendor: z.string().min(1).max(100).optional(),
    model: z.string().min(1).max(200).optional(),
    minMemoryMb: z.number().int().positive().optional(),
    runtime: z.string().min(1).max(50).optional(),
    minRuntimeVersion: z.string().min(1).max(50).optional(),
  })
  .strict();

const ModuleRuntimeSchema = z.discriminatedUnion("mode", [
  z.object({ mode: z.literal("on-demand") }).strict(),
  z.object({ mode: z.literal("service"), systemdUnit: RelativePathSchema }).strict(),
]);

const ModuleDeploymentSchema = z.discriminatedUnion("mode", [
  z.object({ mode: z.literal("replicated"), routing: z.literal("round-robin") }).strict(),
  z.object({ mode: z.literal("singleton") }).strict(),
]);

const JobLifecycleSchema = z
  .object({
    mode: z.literal("job"),
    timeoutMs: z.number().int().min(60_000).max(7 * 24 * 60 * 60 * 1_000),
  })
  .strict();

const PersistentDataSchema = z
  .object({
    storage: z.literal("node"),
    relativePath: RelativePathSchema,
    minFreeMb: z.number().int().positive(),
    retainOnUninstall: z.literal(true),
  })
  .strict();

const IntegerInstallOptionSchema = z
  .object({
    type: z.literal("integer"),
    description: z.string().min(1).max(300),
    minimum: z.number().int(),
    maximum: z.number().int(),
    default: z.number().int().optional(),
  })
  .strict()
  .superRefine((option, context) => {
    if (option.minimum > option.maximum) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["maximum"], message: "must be at least minimum" });
    }
    if (option.default !== undefined && (option.default < option.minimum || option.default > option.maximum)) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["default"], message: "must be within minimum and maximum" });
    }
  });

const StringListInstallOptionSchema = z
  .object({
    type: z.literal("string-list"),
    description: z.string().min(1).max(300),
    minItems: z.number().int().min(1).max(100),
    maxItems: z.number().int().min(1).max(100),
    itemPattern: z.string().min(1).max(200).refine((value) => {
      try {
        new RegExp(value);
        return true;
      } catch {
        return false;
      }
    }, "must be a valid regular expression"),
  })
  .strict()
  .refine((option) => option.minItems <= option.maxItems, {
    path: ["maxItems"],
    message: "must be at least minItems",
  });

const StringInstallOptionSchema = z
  .object({
    type: z.literal("string"),
    description: z.string().min(1).max(300),
    values: z.array(z.string().min(1).max(100)).min(1).max(100),
    default: z.string().min(1).max(100).optional(),
  })
  .strict()
  .superRefine((option, context) => {
    if (new Set(option.values).size !== option.values.length) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["values"], message: "must contain unique values" });
    }
    if (option.default !== undefined && !option.values.includes(option.default)) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["default"], message: "must be one of values" });
    }
  });

const InstallOptionSchema = z.union([IntegerInstallOptionSchema, StringInstallOptionSchema, StringListInstallOptionSchema]);

export const ModuleManifestSchema = z
  .object({
    schemaVersion: z.union([z.literal(1), z.literal(2)]),
    id: z.string().regex(MODULE_ID, "must be a lowercase kebab-case module ID"),
    name: z.string().min(1).max(100),
    version: z.string().regex(SEMVER, "must be a semantic version"),
    description: z.string().min(1).max(500),
    /** Short capability phrases surfaced to the agent so it can route work here without being told the module name. */
    capabilities: z.array(z.string().min(1).max(80)).max(12).default([]),
    entrypoint: z
      .array(z.string().min(1).max(500).refine((value) => !/[\0\r\n]/.test(value), "must not contain control characters"))
      .min(1)
      .max(32),
    compatibility: z
      .object({
        os: z.array(z.string().min(1).max(50)).min(1).optional(),
        architectures: z.array(z.string().min(1).max(50)).min(1).optional(),
        minCores: z.number().int().positive().optional(),
        minRamMb: z.number().int().positive().optional(),
        /** Free space on the root filesystem. Persistent data is sized separately by persistentData.minFreeMb. */
        minDiskMb: z.number().int().positive().optional(),
        requiredCommands: z.array(z.string().regex(COMMAND)).max(100).default([]),
        accelerators: z.array(AcceleratorRequirementSchema).max(16).default([]),
      })
      .strict()
      .default({}),
    packages: z
      .object({
        apt: z.array(z.string().regex(APT_PACKAGE)).max(100).default([]),
      })
      .strict()
      .default({}),
    lifecycle: z
      .object({
        install: RelativePathSchema,
        uninstall: RelativePathSchema,
        execution: JobLifecycleSchema.optional(),
      })
      .strict(),
    persistentData: PersistentDataSchema.optional(),
    installOptions: z.record(z.string().regex(INSTALL_OPTION), InstallOptionSchema).default({}),
    deployment: ModuleDeploymentSchema,
    runtime: ModuleRuntimeSchema.default({ mode: "on-demand" }),
    limits: z
      .object({
        startupMs: z.number().int().min(100).max(120_000).default(10_000),
        callMs: z.number().int().min(100).max(3_600_000).default(30_000),
        maxInputBytes: z.number().int().min(1_024).max(10_000_000).default(262_144),
        maxOutputBytes: z.number().int().min(1_024).max(10_000_000).default(262_144),
      })
      .strict()
      .default({}),
  })
  .strict()
  .superRefine((manifest, context) => {
    if (manifest.schemaVersion === 1) {
      if (manifest.lifecycle.execution !== undefined) {
        context.addIssue({ code: z.ZodIssueCode.custom, path: ["lifecycle", "execution"], message: "requires schemaVersion 2" });
      }
      if (manifest.persistentData !== undefined) {
        context.addIssue({ code: z.ZodIssueCode.custom, path: ["persistentData"], message: "requires schemaVersion 2" });
      }
      if (Object.keys(manifest.installOptions).length > 0) {
        context.addIssue({ code: z.ZodIssueCode.custom, path: ["installOptions"], message: "requires schemaVersion 2" });
      }
      return;
    }
    if (manifest.lifecycle.execution === undefined) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["lifecycle", "execution"], message: "is required for schemaVersion 2" });
    }
  });

export type ModuleManifest = z.infer<typeof ModuleManifestSchema>;
export type AcceleratorRequirement = z.infer<typeof AcceleratorRequirementSchema>;

export function parseModuleManifest(value: unknown, source = "module.json"): ModuleManifest {
  const result = ModuleManifestSchema.safeParse(value);
  if (result.success) return result.data;
  const issues = result.error.issues.map((issue) => `${issue.path.join(".") || "(root)"}: ${issue.message}`).join("; ");
  throw new Error(`Invalid module manifest ${source}: ${issues}`);
}