import { describe, it, expect, beforeAll, vi } from "vitest";

beforeAll(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000";
});

// Stub next/navigation so the page's useRouter() returns a fake we can
// inspect. Defined here (not inline in the test) so the module mock is
// installed before /app/extract/page imports it.
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: (...args: unknown[]) => {
      (globalThis as unknown as { __pushed?: unknown[] }).__pushed = args;
    },
  }),
}));

describe("ExtractPage submission flow", () => {
  it("walks ingest -> transcribe -> extract for a YouTube URL", async () => {
    const { render, screen, fireEvent, waitFor } = await import("@testing-library/react");
    const { default: ExtractPage } = await import("@/app/extract/page");

    const calls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      calls.push(url);

      if (url.endsWith("/content/ingest")) {
        return new Response(
          JSON.stringify({
            id: "job-1",
            kind: "ingest_youtube",
            status: "queued",
            result: null,
            error: null,
          }),
          { status: 202 },
        );
      }
      if (url.endsWith("/jobs/job-1")) {
        return new Response(
          JSON.stringify({
            id: "job-1",
            kind: "ingest_youtube",
            status: "finished",
            result: { content_id: "content-abc" },
            error: null,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith("/content/content-abc/transcribe")) {
        return new Response(
          JSON.stringify({
            id: "job-2",
            kind: "transcribe",
            status: "queued",
            result: null,
            error: null,
          }),
          { status: 202 },
        );
      }
      if (url.endsWith("/jobs/job-2")) {
        return new Response(
          JSON.stringify({
            id: "job-2",
            kind: "transcribe",
            status: "finished",
            result: { transcript_id: "tr-xyz" },
            error: null,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith("/content/content-abc/extract")) {
        return new Response(
          JSON.stringify({
            job_id: "",
            from_cache: true,
            extraction_id: "ext-7",
          }),
          { status: 202 },
        );
      }
      return new Response("nope", { status: 500 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ExtractPage />);

    fireEvent.change(screen.getByLabelText(/source url/i), {
      target: { value: "https://youtu.be/dQw4w9WgXcQ" },
    });
    fireEvent.click(screen.getByRole("button", { name: /analyse/i }));

    await waitFor(
      () => {
        const pushed = (globalThis as unknown as { __pushed?: string[] }).__pushed;
        expect(pushed?.[0]).toBe("/strategies/ext-7");
      },
      { timeout: 5000 },
    );

    // The flow should have hit ingest, jobs/poll, transcribe, jobs/poll, extract
    expect(calls.some((c) => c.endsWith("/content/ingest"))).toBe(true);
    expect(calls.some((c) => c.endsWith("/content/content-abc/transcribe"))).toBe(true);
    expect(calls.some((c) => c.endsWith("/content/content-abc/extract"))).toBe(true);
  });

  it("surfaces an error message when ingest fails", async () => {
    delete (globalThis as unknown as { __pushed?: unknown }).__pushed;
    const { render, screen, fireEvent, waitFor } = await import("@testing-library/react");
    const { default: ExtractPage } = await import("@/app/extract/page");

    const fetchMock = vi.fn(async () => new Response("boom", { status: 500 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<ExtractPage />);
    fireEvent.change(screen.getByLabelText(/source url/i), {
      target: { value: "https://example.com/article" },
    });
    fireEvent.click(screen.getByRole("button", { name: /analyse/i }));

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(/error/i);
    });
  });
});
