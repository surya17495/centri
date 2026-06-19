import { describe, expect, test } from "bun:test"
import { createRoot } from "solid-js"
import { createRefCountMap } from "./refcount"
import { pathKey } from "./path-key"

describe("createRefCountMap", () => {
  test("removes an item after its last owner is disposed", () => {
    const removed: string[] = []
    const map = createRefCountMap(
      (key) => key,
      (key) => removed.push(key),
    )
    const first = createRoot((dispose) => {
      map("/project")
      return dispose
    })
    const second = createRoot((dispose) => {
      map("/project")
      return dispose
    })

    first()
    expect(removed).toEqual([])
    second()
    expect(removed).toEqual(["/project"])
  })

  test("keeps equivalent path consumers until the last owner is disposed", () => {
    const removed: string[] = []
    const map = createRefCountMap(
      (key) => key,
      (key) => removed.push(key),
      pathKey,
    )
    const first = createRoot((dispose) => {
      map("C:\\repo")
      return dispose
    })
    const second = createRoot((dispose) => {
      map("C:/repo/")
      return dispose
    })

    first()
    expect(removed).toEqual([])
    second()
    expect(removed).toEqual(["C:/repo"])
  })

  test("passes the item to the remove callback", () => {
    const itemsRemoved: [string, number][] = []
    const map = createRefCountMap(
      (key) => ({ key, value: 42 }),
      (key, item) => itemsRemoved.push([key, item.value]),
    )
    const first = createRoot((dispose) => {
      map("/project")
      return dispose
    })
    first()
    expect(itemsRemoved).toEqual([["/project", 42]])
  })
})
