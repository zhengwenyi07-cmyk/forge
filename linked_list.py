"""
A comprehensive singly linked list implementation in Python.

Supports:
- Basic operations: append, prepend, insert, delete, search
- Iteration and indexing
- Reversal, sorting (merge sort), deduplication
- Conversion to/from Python lists

This file is a standalone generic singly linked list — it is NOT used by
the Forge Agent core (Agent, Backend, Tools, Context all use Python's
built-in list). It exists as a supplementary data structure exercise.

Time complexity:
  - append / prepend: O(1)  (tail pointer)
  - insert / pop(index): O(n)
  - pop() / remove(value): O(n)
  - __getitem__ / __setitem__: O(n)
  - reverse: O(n)
  - sort (merge sort): O(n log n)
  - __contains__ / index: O(n)
  - __len__: O(1) (tracked by _size)
"""

from __future__ import annotations

# Type generics:
#   Generic[T]  — class is typed with a type parameter
#   TypeVar     — defines T for the type parameter
#   Optional    — Optional[ListNode[T]] for nullable node references
#   Iterator / Generator — __iter__() yields T, nodes() yields ListNode[T]
#   Iterable    — __init__() accepts any iterable, extend() accepts any iterable
#   Callable    — sort(key=Callable[[T], Any])
#   Any         — __eq__(other: Any), __contains__(value: Any)
from typing import (
    Any,
    Callable,
    Generator,
    Generic,
    Iterable,
    Iterator,
    Optional,
    TypeVar,
)

# Type variable bound to nothing — T can be any Python type.
# LinkedList[int], LinkedList[str], LinkedList[MyClass] are all valid.
T = TypeVar("T")


class ListNode(Generic[T]):
    """A single node in a singly linked list."""

    # __slots__ reduces memory overhead per node (no __dict__ per instance).
    # Only two attributes: value (data) and next (pointer).
    __slots__ = ("value", "next")

    def __init__(self, value: T) -> None:
        self.value: T = value                    # Data stored in this node
        self.next: Optional[ListNode[T]] = None  # Pointer to next node (None = tail)

    def __repr__(self) -> str:
        return f"ListNode({self.value!r})"


class LinkedList(Generic[T]):
    """
    Singly linked list with head pointer and optional tail pointer.

    Head pointer (_head) always points to the first node.
    Tail pointer (_tail) points to the last node — enables O(1) append().
    _size tracks element count — O(1) __len__().
    """

    def __init__(self, iterable: Optional[Iterable[T]] = None) -> None:
        # Head pointer: None when list is empty
        self._head: Optional[ListNode[T]] = None
        # Tail pointer: None when list is empty
        # When non-None, self._tail.next is always None
        self._tail: Optional[ListNode[T]] = None
        # Element count, updated by all insertion/deletion methods
        self._size: int = 0

        # Construct from an iterable (list, tuple, generator, or another LinkedList)
        if iterable is not None:
            for val in iterable:
                self.append(val)

    # ==================================================================
    #  Size / emptiness
    # ==================================================================

    def __len__(self) -> int:
        """Return the number of elements (O(1), via _size counter)."""
        return self._size

    def is_empty(self) -> bool:
        """Return True if the list contains no elements."""
        return self._size == 0

    # ==================================================================
    #  Head / tail access
    # ==================================================================

    @property
    def head(self) -> Optional[ListNode[T]]:
        """Return the first node (or None if empty). Useful for iteration."""
        return self._head

    @property
    def tail(self) -> Optional[ListNode[T]]:
        """Return the last node (or None if empty). Enables O(1) append."""
        return self._tail

    # ==================================================================
    #  Insertion
    # ==================================================================

    def prepend(self, value: T) -> None:
        """Insert value at the front of the list. O(1)."""
        node = ListNode(value)
        node.next = self._head
        self._head = node
        if self._tail is None:
            self._tail = node
        self._size += 1

    def append(self, value: T) -> None:
        """Insert value at the end of the list. O(1) via tail pointer."""
        node = ListNode(value)
        if self._tail is None:
            self._head = self._tail = node
        else:
            self._tail.next = node
            self._tail = node
        self._size += 1

    def insert(self, index: int, value: T) -> None:
        """
        Insert value at the given index (0-based).

        Edge cases:
        - index == 0 → prepend(value) (O(1))
        - index == _size → append(value) (O(1))
        - Otherwise → traverse to index-1, splice in new node (O(n))

        Raises IndexError if index < 0 or index > len(list).
        """
        if index < 0 or index > self._size:
            raise IndexError(f"LinkedList index out of range: {index}")

        if index == 0:
            self.prepend(value)
            return

        if index == self._size:
            self.append(value)
            return

        prev = self._node_at(index - 1)
        node = ListNode(value)
        node.next = prev.next
        prev.next = node
        self._size += 1

    def extend(self, other: Iterable[T]) -> None:
        """Append every element from *other* (any iterable). O(k) for k elements."""
        for val in other:
            self.append(val)

    # ==================================================================
    #  Deletion
    # ==================================================================

    def pop(self, index: int = -1) -> T:
        """
        Remove and return the element at *index* (default last).

        Edge cases:
        - Empty list → IndexError
        - index == 0 → O(1) head removal
        - index == _size - 1 → tail removal (still O(n) traversal)
        - index == -1 → maps to _size - 1 (remove last)
        - index out of bounds → IndexError

        Raises IndexError on empty list or bad index.
        """
        if self._head is None:
            raise IndexError("pop from empty list")
        if index < 0:
            index += self._size
        if index < 0 or index >= self._size:
            raise IndexError(f"LinkedList index out of range: {index}")

        if index == 0:
            value = self._head.value
            self._head = self._head.next
            if self._head is None:
                self._tail = None
            self._size -= 1
            return value

        prev = self._node_at(index - 1)
        target = prev.next
        assert target is not None
        value = target.value
        prev.next = target.next
        if target is self._tail:
            self._tail = prev
        self._size -= 1
        return value

    def remove(self, value: T) -> None:
        """
        Remove the first occurrence of *value*.
        Traverses the list looking for a node whose value == value,
        then splices it out by rewiring prev.next.

        Raises ValueError if the value is not present.
        """
        prev: Optional[ListNode[T]] = None
        curr = self._head
        while curr is not None:
            if curr.value == value:
                if prev is None:
                    self._head = curr.next
                else:
                    prev.next = curr.next
                if curr is self._tail:
                    self._tail = prev
                self._size -= 1
                return
            prev = curr
            curr = curr.next
        raise ValueError(f"LinkedList.remove({value!r}): value not found")

    def clear(self) -> None:
        """Remove all elements. O(1) — simply drops head/tail pointers."""
        self._head = None
        self._tail = None
        self._size = 0

    # ==================================================================
    #  Access / search
    # ==================================================================

    def _node_at(self, index: int) -> ListNode[T]:
        """
        Return node at 0-based index (caller validates bounds).
        Private helper — callers (insert, pop, __getitem__, etc.)
        already check bounds before calling this.
        """
        curr = self._head
        for _ in range(index):
            assert curr is not None
            curr = curr.next
        assert curr is not None
        return curr

    def __getitem__(self, index: int) -> T:
        """Return value at 0-based index. Supports negative indexing. O(n)."""
        if not isinstance(index, int):
            raise TypeError(f"list indices must be integers, not {type(index)}")
        if index < 0:
            index += self._size
        if index < 0 or index >= self._size:
            raise IndexError(f"LinkedList index out of range: {index}")
        return self._node_at(index).value

    def __setitem__(self, index: int, value: T) -> None:
        """Set value at 0-based index. Supports negative indexing. O(n)."""
        if index < 0:
            index += self._size
        if index < 0 or index >= self._size:
            raise IndexError(f"LinkedList index out of range: {index}")
        self._node_at(index).value = value

    def index(self, value: T, start: int = 0, stop: Optional[int] = None) -> int:
        """
        Return the first index of *value* (within [start, stop) if given).
        Raises ValueError if not found.
        """
        stop = stop if stop is not None else self._size
        curr = self._head
        i = 0
        while curr is not None and i < stop:
            if i >= start and curr.value == value:
                return i
            curr = curr.next
            i += 1
        raise ValueError(f"{value!r} is not in list")

    def __contains__(self, value: Any) -> bool:
        """Return True if *value* is in the list. O(n)."""
        curr = self._head
        while curr is not None:
            if curr.value == value:
                return True
            curr = curr.next
        return False

    # ==================================================================
    #  Iteration
    # ==================================================================

    def __iter__(self) -> Iterator[T]:
        """Yield each node's value from head to tail. O(n)."""
        curr = self._head
        while curr is not None:
            yield curr.value
            curr = curr.next

    def nodes(self) -> Generator[ListNode[T], None, None]:
        """
        Yield ListNode objects (useful for debugging).
        Unlike __iter__() which yields values, this yields the node objects
        themselves so you can inspect next pointers.
        """
        curr = self._head
        while curr is not None:
            yield curr
            curr = curr.next

    # ==================================================================
    #  Slicing
    # ==================================================================

    def __getitem_slice(self, s: slice) -> LinkedList[T]:
        """Get a slice: convert to list, slice, convert back."""
        return LinkedList(list(self)[s])

    def __setitem_slice(self, s: slice, values: Iterable[T]) -> None:
        """Set a slice: convert to list, slice-assign, rebuild list."""
        vals = list(values)
        as_list = list(self)
        as_list[s] = vals
        self.clear()
        self.extend(as_list)

    # ==================================================================
    #  Reversal
    # ==================================================================

    def reverse(self) -> None:
        """
        Reverse the list in-place. O(n).
        Algorithm: iterate through the list, reversing each node's next
        pointer to point to the previous node. After the loop, swap
        head and tail.
        """
        prev: Optional[ListNode[T]] = None
        curr = self._head
        self._tail = curr
        while curr is not None:
            nxt = curr.next      # Save next before overwriting
            curr.next = prev     # Reverse the pointer
            prev = curr          # Move prev forward
            curr = nxt           # Move curr forward
        self._head = prev

    # ==================================================================
    #  Sorting (merge sort – O(n log n), stable)
    #  Iterative bottom-up merge sort (no recursion, O(log n) stack space).
    #  Start with sublists of size 1, repeatedly merge adjacent pairs,
    #  doubling sublist size each pass.
    # ==================================================================

    def sort(self, key: Optional[Callable[[T], Any]] = None, reverse: bool = False) -> None:
        """
        Sort the list in-place using iterative merge sort.

        Stable sort (equal elements retain their relative order).
        O(n log n) time, O(1) extra space (excluding recursion stack).

        Args:
            key:     Optional function that extracts comparison key from element.
                     Defaults to identity (x -> x). Example: key=lambda x: x.name
            reverse: If True, sort in descending order (reverses after sort).
        """
        if self._size < 2:
            return

        # Break into sorted runs and repeatedly merge
        key_fn = key if key is not None else (lambda x: x)

        def _merge(
            left: Optional[ListNode[T]],
            right: Optional[ListNode[T]],
        ) -> tuple[Optional[ListNode[T]], Optional[ListNode[T]]]:
            """
            Merge two sorted lists, return (new_head, new_tail).
            Uses a dummy node to simplify the merge loop.
            """
            dummy = ListNode(None)  # type: ignore[arg-type]
            tail: ListNode[T] = dummy
            while left is not None and right is not None:
                # Stability: <= keeps left-before-right when keys equal
                if key_fn(left.value) <= key_fn(right.value):
                    tail.next = left
                    left = left.next
                else:
                    tail.next = right
                    right = right.next
                tail = tail.next
            tail.next = left if left is not None else right
            # Walk to new tail
            while tail.next is not None:
                tail = tail.next
            return dummy.next, tail

        # Iterative merge sort: split list into sub-lists of size 1,2,4,...
        head = self._head
        step = 1
        while step < self._size:
            left: Optional[ListNode[T]] = head
            head = None
            tail: Optional[ListNode[T]] = None
            while left is not None:
                # Split into two halves of size `step`
                right = left
                for _ in range(step):
                    if right is None:
                        break
                    right = right.next
                # Detach the first half
                second = right
                first = left
                # Detach second half start
                right_start = second
                for _ in range(step):
                    if right_start is None:
                        break
                    right_start = right_start.next
                # Detach first half end
                left_end = left
                for _ in range(step - 1):
                    if left_end is None:
                        break
                    left_end = left_end.next

                if left_end is not None:
                    left_end.next = None

                # Detach second half end
                if second is not None:
                    right_end = second
                    for _ in range(step - 1):
                        if right_end is None or right_end.next is None:
                            break
                        right_end = right_end.next
                    if right_end is not None:
                        # Save next chunk start before detaching
                        nxt = right_end.next
                        right_end.next = None
                    else:
                        nxt = None
                else:
                    nxt = None

                merged_head, merged_tail = _merge(first, second)
                if head is None:
                    head = merged_head
                if tail is not None:
                    tail.next = merged_head
                tail = merged_tail

                left = nxt

            self._head = head
            self._tail = tail
            step <<= 1

        if reverse:
            self.reverse()

    # ==================================================================
    #  Copy
    # ==================================================================

    def copy(self) -> LinkedList[T]:
        """Return a shallow copy (elements are not deep-copied). O(n)."""
        return LinkedList(self)

    __copy__ = copy

    # ==================================================================
    #  Conversion
    # ==================================================================

    def to_list(self) -> list[T]:
        """Convert to Python built-in list. O(n)."""
        return list(self)

    @classmethod
    def from_list(cls, items: list[T]) -> LinkedList[T]:
        """Build a LinkedList from a Python list. O(n)."""
        return cls(items)

    # ==================================================================
    #  String representation
    # ==================================================================

    def __repr__(self) -> str:
        """Developer-friendly repr: LinkedList([1, 2, 3])"""
        items = [repr(v) for v in self]
        return f"LinkedList([{', '.join(items)}])"

    def __str__(self) -> str:
        """User-friendly string: 1 -> 2 -> 3"""
        return " -> ".join(str(v) for v in self)

    # ==================================================================
    #  Equality
    # ==================================================================

    def __eq__(self, other: Any) -> bool:
        """
        Deep equality: two LinkedLists are equal iff they have the same
        length and all corresponding elements are equal.
        Returns NotImplemented (not False) when compared to non-LinkedList,
        letting Python try the other object's __eq__ or fall back to identity.
        """
        if not isinstance(other, LinkedList):
            return NotImplemented
        if len(self) != len(other):
            return False
        return all(a == b for a, b in zip(self, other))
