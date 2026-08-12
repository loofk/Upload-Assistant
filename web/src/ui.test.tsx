import {cleanup, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, expect, it, vi} from "vitest";
import {useState} from "react";
import {Drawer} from "./ui";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("focuses drawer fields, protects dirty edits on Escape, and restores focus", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<DrawerHarness/>);
  const opener = screen.getByRole("button", {name: "打开"});
  await userEvent.click(opener);
  const field = screen.getByLabelText("名称");
  await waitFor(() => expect(field).toHaveFocus());
  await userEvent.type(field, "修改");
  await userEvent.keyboard("{Escape}");
  expect(confirm).toHaveBeenCalledWith("有尚未保存的修改，确认关闭？");
  expect(screen.getByRole("dialog", {name: "编辑配置"})).toBeInTheDocument();

  confirm.mockReturnValue(true);
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", {name: "编辑配置"})).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
});

function DrawerHarness() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  return <><button onClick={() => setOpen(true)}>打开</button><Drawer open={open} title="编辑配置" dirty={Boolean(value)} onClose={() => setOpen(false)}><label>名称<input value={value} onChange={(event) => setValue(event.target.value)}/></label></Drawer></>;
}
