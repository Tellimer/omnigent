import { afterEach, describe, expect, it } from "vitest";
import {
  readLastTellimerRepository,
  repositoryPresetById,
  repositoryPresetFor,
  TELLIMER_REPOSITORIES,
  writeLastTellimerRepository,
} from "./tellimerRepositories";

afterEach(() => localStorage.clear());

describe("Tellimer repository presets", () => {
  it("maps each platform project to its real repository and branch", () => {
    expect(TELLIMER_REPOSITORIES.map(({ id, url, branch }) => ({ id, url, branch }))).toEqual([
      {
        id: "agent-platform",
        url: "https://github.com/Tellimer/agent-platform.git",
        branch: "main",
      },
      { id: "sentinel-v4", url: "https://github.com/Tellimer/spectra.git", branch: "staging" },
      { id: "insights-v4", url: "https://github.com/Tellimer/articles.git", branch: "master" },
    ]);
  });

  it("recognizes equivalent GitHub URLs and their configured branches", () => {
    expect(repositoryPresetFor("https://github.com/Tellimer/spectra", "staging")?.id).toBe(
      "sentinel-v4",
    );
    expect(repositoryPresetFor("https://github.com/Tellimer/spectra.git/", "staging")?.id).toBe(
      "sentinel-v4",
    );
    expect(repositoryPresetFor("https://github.com/Tellimer/spectra", "main")).toBeNull();
  });

  it("remembers an explicit project selection without guessing the first project", () => {
    expect(readLastTellimerRepository()).toBeNull();
    writeLastTellimerRepository("insights-v4");
    expect(readLastTellimerRepository()?.id).toBe("insights-v4");
    expect(repositoryPresetById("unknown")).toBeNull();
  });
});
