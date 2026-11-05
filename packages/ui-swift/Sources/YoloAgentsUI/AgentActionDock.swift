import SwiftUI

/// A product-neutral destination displayed by ``AgentActionDock``.
///
/// Agent apps own the identifiers, labels and actions. The shared dock only
/// renders those values and reports the selected identifier.
public struct AgentActionDockItem<ID: Hashable & Sendable>: Identifiable, Hashable, Sendable {
    public let id: ID
    public let title: String
    public let compactTitle: String
    public let systemImage: String
    public let accessibilityIdentifier: String

    public init(
        id: ID,
        title: String,
        compactTitle: String? = nil,
        systemImage: String,
        accessibilityIdentifier: String? = nil
    ) {
        self.id = id
        self.title = title
        self.compactTitle = compactTitle ?? title
        self.systemImage = systemImage
        self.accessibilityIdentifier = accessibilityIdentifier ?? String(describing: id)
    }
}

#if os(iOS)
/// A reusable iOS 26 bottom action dock backed by native Liquid Glass.
///
/// Unlike a `TabView`, this component does not own navigation or require a
/// permanent selection. Its owner decides whether selection represents a
/// destination, a presented action, or no active item at all.
@available(iOS 26.0, *)
public struct AgentActionDock<ID: Hashable & Sendable>: View {
    @Binding private var selection: ID?
    private let items: [AgentActionDockItem<ID>]
    private let tint: Color
    private let selectedForeground: Color
    private let onSelect: (ID) -> Void

    @Namespace private var glassNamespace
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .body) private var bubbleSize: CGFloat = 58
    @ScaledMetric(relativeTo: .body) private var itemHeight: CGFloat = 62

    public init(
        selection: Binding<ID?>,
        items: [AgentActionDockItem<ID>],
        tint: Color = .accentColor,
        selectedForeground: Color = .white,
        onSelect: @escaping (ID) -> Void
    ) {
        self._selection = selection
        self.items = items
        self.tint = tint
        self.selectedForeground = selectedForeground
        self.onSelect = onSelect
    }

    public var body: some View {
        GlassEffectContainer(spacing: 8) {
            HStack(spacing: 4) {
                ForEach(items) { item in
                    dockButton(for: item)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity)
            .glassEffect(.regular, in: Capsule())
        }
        .shadow(color: .black.opacity(0.14), radius: 18, y: 8)
        .padding(.horizontal, 18)
        .padding(.top, 6)
        .padding(.bottom, 8)
        .sensoryFeedback(.selection, trigger: selection)
        .accessibilityElement(children: .contain)
    }

    private func dockButton(for item: AgentActionDockItem<ID>) -> some View {
        let isSelected = selection == item.id

        return Button {
            select(item.id)
        } label: {
            ZStack {
                if isSelected {
                    Circle()
                        .fill(.clear)
                        .frame(width: bubbleSize, height: bubbleSize)
                        .glassEffect(.regular.tint(tint).interactive(), in: Circle())
                        .glassEffectID("agent-action-dock-selection", in: glassNamespace)
                        .glassEffectTransition(.matchedGeometry)
                        .shadow(color: tint.opacity(0.28), radius: 10, y: 4)
                }

                VStack(spacing: isSelected ? 1 : 3) {
                    Image(systemName: item.systemImage)
                        .font(.system(size: isSelected ? 18 : 20,
                                      weight: isSelected ? .semibold : .medium))
                        .symbolEffect(.bounce, value: isSelected)
                    Text(isSelected ? item.compactTitle : item.title)
                        .font(.caption2.weight(isSelected ? .semibold : .medium))
                        .lineLimit(1)
                        .minimumScaleFactor(0.72)
                }
                .foregroundStyle(isSelected ? selectedForeground : Color.primary)
                .padding(.horizontal, 4)
            }
            .frame(maxWidth: .infinity, minHeight: itemHeight)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(item.title)
        .accessibilityIdentifier(item.accessibilityIdentifier)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }

    private func select(_ id: ID) {
        let animation = reduceMotion
            ? nil
            : Animation.spring(response: 0.32, dampingFraction: 0.82)
        withAnimation(animation) {
            selection = id
        }
        onSelect(id)
    }
}
#endif
