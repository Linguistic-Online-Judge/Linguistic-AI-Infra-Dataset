import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ChallengeNotFound from "@/app/challenges/[challengeId]/not-found";
import ChallengesError from "@/app/challenges/error";
import { CatalogLoading } from "@/components/catalog-loading";

afterEach(cleanup);

describe("catalog route states", () => {
  it("announces loading progress", () => {
    render(<CatalogLoading />);

    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
    expect(
      screen.getByRole("heading", { name: "正在加载公开题目" }),
    ).toBeInTheDocument();
  });

  it("offers one working retry after a service failure", () => {
    const retry = vi.fn();
    render(<ChallengesError retry={retry} />);

    fireEvent.click(screen.getByRole("button", { name: "重新加载题目" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("returns visitors from an unknown challenge to the catalog", () => {
    render(<ChallengeNotFound />);

    expect(screen.getByRole("heading", { name: "未找到该题目" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回题目列表" })).toHaveAttribute(
      "href",
      "/challenges",
    );
  });
});
