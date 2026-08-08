import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {beforeEach, describe, expect, it} from "vitest";
import App from "./App";

describe("App authentication boundary", () => {
  beforeEach(() => sessionStorage.clear());

  it("keeps the API token entry local to the current session", async () => {
    render(<App />);
    expect(screen.getByRole("heading", {name: "转种工作台"})).toBeInTheDocument();
    const token = screen.getByLabelText("API Token");
    expect(token).toHaveAttribute("type", "password");
    await userEvent.type(token, "ua_test-token-value-that-is-long-enough");
    expect(localStorage.getItem("ua.v2.api-token")).toBeNull();
  });
});
