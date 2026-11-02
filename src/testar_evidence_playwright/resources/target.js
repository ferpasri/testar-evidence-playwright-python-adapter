(elements) => {
    if (elements.length !== 1 || window !== window.top) {
        return null;
    }
    const parts = [];
    let current = elements[0];
    while (current) {
        const parent = current.parentNode;
        const siblings = parent?.children;
        if (!siblings) {
            return null;
        }
        const index = Array.prototype.indexOf.call(siblings, current);
        parts.unshift(current.tagName.toLowerCase() + "[" + index + "]");
        if (parent === document) {
            return "#document/" + parts.join("/");
        }
        if (parent instanceof ShadowRoot) {
            parts.unshift("#shadow-root");
            current = parent.host;
        } else {
            current = parent;
        }
    }
    return null;
}
