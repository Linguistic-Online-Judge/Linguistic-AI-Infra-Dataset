import axe from "axe-core";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ChallengeList } from "@/components/challenge-list";
import { alphaChallenge, zetaChallenge } from "@/tests/fixtures";

afterEach(cleanup);

describe("ChallengeList", () => {
  it("shows registered challenges in stable order with text status", () => {
    render(<ChallengeList challenges={[zetaChallenge, alphaChallenge]} />);

    const records = screen.getAllByRole("article");
    expect(
      within(records[0]).getByRole("link", { name: zetaChallenge.title }),
    ).toHaveAttribute("href", `/challenges/${zetaChallenge.challenge_id}`);
    expect(
      within(records[1]).getByRole("link", { name: alphaChallenge.title }),
    ).toBeInTheDocument();
    expect(screen.getByText("提交已开放")).toBeInTheDocument();
    expect(screen.getByText("提交未开放")).toBeInTheDocument();
    expect(screen.getByText("1,200 个样本")).toBeInTheDocument();
  });

  it("shows a useful empty state without a dead action", () => {
    render(<ChallengeList challenges={[]} />);

    expect(
      screen.getByRole("heading", { name: "暂无公开题目" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    const { container } = render(
      <ChallengeList challenges={[alphaChallenge, zetaChallenge]} />,
    );

    const result = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.violations).toEqual([]);
  });
});
