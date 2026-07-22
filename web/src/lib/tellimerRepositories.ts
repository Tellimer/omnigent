export interface TellimerRepositoryPreset {
  id: "agent-platform" | "sentinel-v4" | "insights-v4";
  label: string;
  description: string;
  url: string;
  branch: string;
}

export const TELLIMER_REPOSITORIES: readonly TellimerRepositoryPreset[] = [
  {
    id: "agent-platform",
    label: "Agent Platform",
    description: "Deno control plane, docs, MCP registry and Railway services",
    url: "https://github.com/Tellimer/agent-platform.git",
    branch: "main",
  },
  {
    id: "sentinel-v4",
    label: "Sentinel v4",
    description: "Spectra monorepo · apps/sentinel-v4",
    url: "https://github.com/Tellimer/spectra.git",
    branch: "staging",
  },
  {
    id: "insights-v4",
    label: "Insights v4",
    description: "Articles monorepo · apps/insights-v4",
    url: "https://github.com/Tellimer/articles.git",
    branch: "master",
  },
] as const;

const STORAGE_KEY = "tellimer:last-repository-preset";

export function repositoryPresetById(id: string): TellimerRepositoryPreset | null {
  return TELLIMER_REPOSITORIES.find((repository) => repository.id === id) ?? null;
}

export function repositoryPresetFor(url: string, branch: string): TellimerRepositoryPreset | null {
  const normalizedUrl = url
    .trim()
    .replace(/\/$/, "")
    .replace(/\.git$/, "")
    .toLowerCase();
  const normalizedBranch = branch.trim();
  return (
    TELLIMER_REPOSITORIES.find(
      (repository) =>
        repository.url.replace(/\.git$/, "").toLowerCase() === normalizedUrl &&
        repository.branch === normalizedBranch,
    ) ?? null
  );
}

export function readLastTellimerRepository(): TellimerRepositoryPreset | null {
  try {
    return repositoryPresetById(window.localStorage.getItem(STORAGE_KEY) ?? "");
  } catch {
    return null;
  }
}

export function writeLastTellimerRepository(id: string): void {
  const repository = repositoryPresetById(id);
  if (!repository) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, repository.id);
  } catch {
    // Preference persistence is best-effort in restricted browser contexts.
  }
}
