import { test, expect } from "@playwright/test";

test("real API restores the report and renders only recorded evidence", async ({ page, request }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const setup = await (await request.get("http://127.0.0.1:18092/test/session")).json();
  await page.addInitScript(session => {
    localStorage.setItem("bigdipper.auth.session.v2", JSON.stringify(session));
  }, setup.session);
  await page.goto(`/workspace/${setup.workspaceId}`);
  await expect(page.getByText("Browser fixture report.", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Evidence Graph", exact: true }).click();
  await expect(page.getByRole("button", { name: "多模证据子图", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "多模证据子图", exact: true }).click();
  await expect(page.locator(".graph-canvas canvas").first()).toBeVisible();
  const graph = await page.evaluate(() => {
    const canvas = document.querySelector(".graph-canvas") as HTMLElement & { _cyreg?: { cy?: { nodes: () => { length: number }; edges: () => { map: (fn: (edge: { data: () => Record<string, unknown> }) => unknown) => unknown[] } } } };
    const cy = canvas?._cyreg?.cy;
    return { count: cy?.nodes().length, edges: cy?.edges().map(e => e.data()) };
  });
  expect(graph.count).toBeGreaterThanOrEqual(6);
  expect(graph.edges?.some((e: any) => String(e.id).startsWith("display-similar"))).toBeFalsy();
  expect(graph.edges?.some((e: any) => e.type === "similar")).toBeFalsy();
  await page.screenshot({ path: testInfo.outputPath("evidence-subgraph.png"), fullPage: true });
  // Tap a real recorded citation using Cytoscape's event API.
  await page.evaluate(() => {
    const cy = (document.querySelector(".graph-canvas") as any)._cyreg.cy;
    cy.edges('[type="cites"]').first().emit("tap");
  });
  await expect(page.getByRole("complementary", { name: "Evidence detail" })).toBeVisible();
  await expect(page.getByText("Recorded facts and provenance")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("evidence-provenance.png"), fullPage: true });
  await page.reload();
  await expect(page.getByRole("button", { name: "Evidence Graph", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "多模证据子图", exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
