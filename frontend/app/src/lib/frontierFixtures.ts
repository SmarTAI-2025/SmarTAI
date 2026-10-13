export const QUESTION_FIXTURE = "/frontier-demo/live/question_source.pdf";
export const SUBMISSION_FIXTURE = "/frontier-demo/live/submissions_raw.zip";
export const DEMO_SCORE_POLICY = "Q1: 5 points; Q2: 8 points; Q3: 7 points; Q4: 10 points.";
const FIXTURE_MANIFEST = "/frontier-demo/manifest.json";
export const demoLockedText = (locale: string) => locale === "zh-CN"
  ? "演示任务不可修改这些预设信息或替换文件；真实任务可修改。批改风格可在批改设置中调整。"
  : "These preset details and files are fixed in the demo. They are editable in your own tasks. You can adjust grading style in Grading Settings.";
export async function fixtureFile(url: string, filename: string, type: string) {
  const [response, manifest] = await Promise.all([
    fetch(url, { cache: "no-store" }),
    fixtureManifest(),
  ]);
  if (!response.ok) throw new Error(`Could not load the synthetic fixture ${filename} (${response.status}).`);
  const blob = await response.blob();
  const relativePath = url.replace(/^\/frontier-demo\//, "");
  const entry = manifest.assets.find((asset) => asset.path === relativePath);
  if (!entry) throw new Error(`The fixture manifest does not contain ${filename}.`);
  const digest = await sha256Hex(await blob.arrayBuffer());
  if (digest !== entry.sha256) throw new Error(`Fixture integrity check failed for ${filename}.`);
  return new File([blob], filename, { type, lastModified: 0 });
}

let manifestPromise: Promise<FixtureManifest> | null = null;

function fixtureManifest() {
  manifestPromise ??= fetch(FIXTURE_MANIFEST, { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) throw new Error(`Could not load the fixture manifest (${response.status}).`);
      const body = await response.json() as Partial<FixtureManifest>;
      if (!Array.isArray(body.assets)) throw new Error("The fixture manifest is invalid.");
      return { assets: body.assets };
    })
    .catch((error) => {
      manifestPromise = null;
      throw error;
    });
  return manifestPromise;
}

async function sha256Hex(value: ArrayBuffer) {
  const digest = await crypto.subtle.digest("SHA-256", value);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

interface FixtureManifest {
  assets: Array<{ path: string; bytes: number; sha256: string }>;
}
