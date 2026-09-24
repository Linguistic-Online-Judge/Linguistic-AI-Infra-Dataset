import axe from "axe-core";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ChallengeDetailView } from "@/components/challenge-detail";
import { alphaDetail } from "@/tests/fixtures";

afterEach(cleanup);

describe("ChallengeDetailView", () => {
  it("presents the public evaluation record without a fake submission action", () => {
    render(<ChallengeDetailView challenge={alphaDetail} />);

    expect(
      screen.getByRole("heading", { level: 1, name: alphaDetail.title }),
    ).toBeInTheDocument();
    expect(screen.getByText("通用词性标注")).toBeInTheDocument();
    expect(screen.getByText("微平均准确率")).toBeInTheDocument();
    expect(screen.getByText("提交已开放")).toBeInTheDocument();
    expect(screen.getByText(/提交功能仅对已登录用户开放/)).toBeInTheDocument();
    expect(screen.getByText(alphaDetail.dataset_sha256)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /提交/ })).not.toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    const { container } = render(<ChallengeDetailView challenge={alphaDetail} />);

    const result = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.violations).toEqual([]);
  });
});
