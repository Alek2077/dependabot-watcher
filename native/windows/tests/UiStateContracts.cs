using System;
using System.IO;
using System.Reflection;
using System.Threading;

internal static class UiStateContracts
{
    internal static void Run(Assembly assembly)
    {
        Exception? failure = null;
        var thread = new Thread(() => {
            try {
                var clientType = assembly.GetType("WatcherNativeProof.BackendClient", true)!;
                var client = Activator.CreateInstance(clientType, BindingFlags.Instance | BindingFlags.NonPublic,
                    null, new object[] { Array.Empty<string>(), true }, null)!;
                var type = assembly.GetType("WatcherNativeProof.BackendQueueWindow", true)!;
                var window = Activator.CreateInstance(type, BindingFlags.Instance | BindingFlags.NonPublic,
                    null, new object[] { client }, null)!;
                object Field(string name) => type.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
                var emptyFrame = System.Text.Json.Nodes.JsonNode.Parse("{\"capabilities\":{\"github_mutations\":false,\"model_calls\":false},\"next_request_sequence\":1,\"proposals\":[],\"plan_id\":null,\"evidence_status\":\"complete\",\"supervisor_context_digests\":{},\"reviews\":[],\"history\":[]}")!;
                type.GetMethod("Apply", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, new object[] { emptyFrame });
                if (((System.Windows.UIElement)Field("_actions")).Visibility != System.Windows.Visibility.Collapsed
                    || ((System.Windows.UIElement)Field("_emptySelection")).Visibility != System.Windows.Visibility.Visible)
                    throw new InvalidDataException("Initial empty queue exposed stale agent controls.");
                var targets = (System.Windows.FrameworkElement)Field("_localSyncTargets");
                if (!ReferenceEquals(targets.Parent, Field("_localSyncPanel")))
                    throw new InvalidDataException("Checkout rows must share inspector scrolling, not capture wheel input in a nested viewport.");
                var panel = (System.Windows.Controls.StackPanel)Field("_localSyncPanel");
                type.GetProperty("_refreshRequired", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, true);
                type.GetMethod("SetBusy", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, new object[] { false });
                if (panel.IsEnabled || ((System.Windows.Controls.Button)Field("_saveMessage")).IsEnabled)
                    throw new InvalidDataException("Unverified queue enables checkout updates or saved decisions.");
                type.GetProperty("_refreshRequired", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, false);
                var investigation = new System.Threading.CancellationTokenSource();
                type.GetField("_investigationCancellation", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, investigation);
                type.GetMethod("SetBusy", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, new object[] { false });
                if (((System.Windows.Controls.WrapPanel)Field("_decisions")).IsEnabled
                    || ((System.Windows.Controls.Button)Field("_saveMessage")).IsEnabled)
                    throw new InvalidDataException("Live investigation enables context-mutating local decisions.");
                type.GetField("_investigationCancellation", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, null);
                investigation.Dispose();
                type.GetMethod("SetBusy", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, new object[] { false });
                var workspaces = (System.Windows.Controls.TabControl)Field("_workspaces");
                var selectedWorkspace = (System.Windows.Controls.TabItem)Field("_selectedWorkspace");
                var maintenanceWorkspace = (System.Windows.Controls.TabItem)Field("_maintenanceWorkspace");
                if (!ReferenceEquals(workspaces.SelectedItem, selectedWorkspace)
                    || !ReferenceEquals(selectedWorkspace.Content, Field("_detailsScroll"))
                    || !ReferenceEquals(maintenanceWorkspace.Content, Field("_maintenanceScroll")))
                    throw new InvalidDataException("Selected item and historical maintenance must have independent workspaces, defaulting to the selected item.");
                workspaces.SelectedItem = maintenanceWorkspace;
                panel.Visibility = System.Windows.Visibility.Visible;
                var rows = (System.Windows.Controls.StackPanel)targets;
                var row = new System.Windows.Controls.TextBlock { Text = "Checkout status", Height = 1000 };
                rows.Children.Add(row);
                var root = (System.Windows.FrameworkElement)((System.Windows.Window)window).Content;
                root.Measure(new System.Windows.Size(1080, 840));
                root.Arrange(new System.Windows.Rect(0, 0, 1080, 840));
                root.UpdateLayout();
                var scroll = (System.Windows.Controls.ScrollViewer)Field("_maintenanceScroll");
                var wheel = new System.Windows.Input.MouseWheelEventArgs(System.Windows.Input.Mouse.PrimaryDevice, 0, -120) {
                    RoutedEvent = System.Windows.Input.Mouse.MouseWheelEvent
                };
                row.RaiseEvent(wheel);
                root.UpdateLayout();
                if (!wheel.Handled || scroll.VerticalOffset <= 0)
                    throw new InvalidDataException("Wheel input over checkout status did not scroll the inspector.");
                workspaces.SelectedItem = selectedWorkspace;
                var frame = System.Text.Json.Nodes.JsonNode.Parse("{\"preview\":{\"command\":\"cat synthetic-fixture.txt\",\"cwd\":\"/temporary-fixture\"}}")!;
                var preview = (System.Windows.Controls.StackPanel)type.GetMethod("PermissionPreview", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, new object[] { frame })!;
                preview.Children.Add(new System.Windows.Controls.TextBlock { Text = "More request detail", Height = 1000 });
                var outer = new System.Windows.Controls.ScrollViewer { Content = preview, Height = 150, Width = 400 };
                outer.Measure(new System.Windows.Size(400, 150)); outer.Arrange(new System.Windows.Rect(0, 0, 400, 150)); outer.UpdateLayout();
                var field = (System.Windows.Controls.TextBox)preview.Children[1];
                var fieldWheel = new System.Windows.Input.MouseWheelEventArgs(System.Windows.Input.Mouse.PrimaryDevice, 0, -120) {
                    RoutedEvent = System.Windows.Input.Mouse.PreviewMouseWheelEvent
                };
                field.RaiseEvent(fieldWheel); outer.UpdateLayout();
                if (!fieldWheel.Handled || outer.VerticalOffset <= 0)
                    throw new InvalidDataException("Readonly command field trapped wheel input at its scrolling boundary.");
                foreach (var name in new[] { "_outcome", "_detail", "_savedMessages", "_latestQuestion", "_latestReply", "_replySource", "_replyFreshness" })
                    Field(name).GetType().GetProperty("Text")!.SetValue(Field(name), "Stale item content");
                var attention = Field("_permissionAttention");
                var visibility = attention.GetType().GetProperty("Visibility")!;
                visibility.SetValue(attention, Enum.Parse(visibility.PropertyType, "Visible"));
                var access = (System.Windows.Controls.Expander)Field("_accessRequests");
                var details = (System.Windows.Controls.ScrollViewer)Field("_detailsScroll");
                access.IsExpanded = true;
                if (details.Visibility != System.Windows.Visibility.Collapsed || access.Visibility != System.Windows.Visibility.Visible)
                    throw new InvalidDataException("Access request workspace did not replace the item inspector.");
                access.IsExpanded = false;
                if (details.Visibility != System.Windows.Visibility.Visible || access.Visibility != System.Windows.Visibility.Collapsed)
                    throw new InvalidDataException("Returning to item did not restore the inspector.");
                access.IsExpanded = true;
                ((System.Windows.Controls.StackPanel)Field("_permissions")).Children.Add(new System.Windows.Controls.TextBlock { Text = "Stale request" });
                type.GetProperty("_refreshRequired", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, true);
                if (access.IsExpanded || ((System.Windows.Controls.StackPanel)Field("_permissions")).Children.Count != 0)
                    throw new InvalidDataException("Unverified queue frame retained a stale permission overlay.");
                access.IsExpanded = true;
                type.GetMethod("SelectProposal", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window,null);
                if (access.IsExpanded || access.Visibility != System.Windows.Visibility.Collapsed)
                    throw new InvalidDataException("Retired selection kept stale permission workspace.");
                if (((System.Windows.UIElement)Field("_actions")).Visibility != System.Windows.Visibility.Collapsed
                    || ((System.Windows.UIElement)Field("_emptySelection")).Visibility != System.Windows.Visibility.Visible)
                    throw new InvalidDataException("Empty selection retained unrelated agent/action controls.");
                foreach (var name in new[] { "_outcome", "_detail", "_savedMessages", "_latestQuestion", "_latestReply", "_replySource", "_replyFreshness" })
                    if ((string)Field(name).GetType().GetProperty("Text")!.GetValue(Field(name))! != "")
                        throw new InvalidDataException("Retired selection kept stale item-specific content.");
                if (visibility.GetValue(attention)!.ToString() != "Collapsed")
                    throw new InvalidDataException("Retired selection kept stale access CTA.");
                foreach (var name in new[] { "_completionStatus", "_localSyncStatus", "_actionSummary", "_latestReply", "_handling", "_nextStep", "_launchPolicy", "_summary", "_launchProgress", "_messageStatus", "_savedMessages", "_historyStatus", "_replyFreshness", "_agentReason", "_detail", "_confirmationText", "_agentReplies", "_latestQuestion", "_outcome" })
                {
                    workspaces.SelectedItem = name is "_completionStatus" or "_localSyncStatus" or "_actionSummary"
                        ? maintenanceWorkspace : selectedWorkspace;
                    root.UpdateLayout();
                    var text = (System.Windows.Controls.TextBlock)Field(name);
                    var grouped = name is "_latestReply" or "_actionSummary" or "_completionStatus" or "_localSyncStatus" or "_handling" or "_nextStep" or "_launchPolicy";
                    var region = grouped ? (System.Windows.FrameworkElement)text.Parent : text;
                    if (region.Parent is not System.Windows.Controls.Grid block || block.Children.Count != 2
                        || block.Children[1] is not System.Windows.Controls.Button icon || icon.Width > 28
                        || icon.Opacity != 0 || icon.IsHitTestVisible
                        || icon.VerticalAlignment != System.Windows.VerticalAlignment.Top
                        || icon.HorizontalAlignment != System.Windows.HorizontalAlignment.Right)
                        throw new InvalidDataException("Copy affordance must be an initially hidden corner icon, not a toolbar.");
                    if (!grouped)
                    {
                        text.Text = "Synthetic copyable text";
                        if (block.Visibility != System.Windows.Visibility.Visible)
                            throw new InvalidDataException("Nonempty text did not reveal its copy region.");
                    }
                    block.Measure(new System.Windows.Size(500, double.PositiveInfinity));
                    block.Arrange(new System.Windows.Rect(0, 0, 500, block.DesiredSize.Height));
                    var gap = System.Windows.Media.VisualTreeHelper.HitTest(block,
                        new System.Windows.Point(block.ActualWidth - 28, 5));
                    if (gap is null || !ReferenceEquals(gap.VisualHit, block))
                        throw new InvalidDataException($"Copy icon's reserved gap is not a continuous hover target: {name}.");
                    if (grouped)
                    {
                        if (region is not System.Windows.Controls.StackPanel content || content.Children.Count < 2
                            || !ReferenceEquals(content.Children[content.Children.Count - 1], text))
                            throw new InvalidDataException("Copy icon must belong to the whole labelled text block.");
                        var heading = (System.Windows.Controls.TextBlock)content.Children[0];
                        heading.Text = "Current heading";
                        text.Text = "Current body";
                        var copyPayload = type.GetMethod("CopyRegionText", BindingFlags.Static | BindingFlags.NonPublic)!;
                        var copied = (string)copyPayload.Invoke(null, new object[] { region })!;
                        if (!copied.StartsWith("Current heading\n", StringComparison.Ordinal) || !copied.EndsWith("Current body", StringComparison.Ordinal))
                            throw new InvalidDataException("Grouped copy payload omitted its current heading or body.");
                        heading.Text = "Updated heading"; text.Text = "Updated body";
                        copied = (string)copyPayload.Invoke(null, new object[] { region })!;
                        if (!copied.StartsWith("Updated heading\n", StringComparison.Ordinal) || !copied.EndsWith("Updated body", StringComparison.Ordinal)
                            || copied.Contains("Current body", StringComparison.Ordinal))
                            throw new InvalidDataException("Grouped copy payload retained stale field text.");
                        heading.Visibility = System.Windows.Visibility.Collapsed;
                        if (((string)copyPayload.Invoke(null, new object[] { region })!).Contains("Updated heading", StringComparison.Ordinal))
                            throw new InvalidDataException("Grouped copy payload included hidden content.");
                        heading.Visibility = System.Windows.Visibility.Visible;
                        block.Measure(new System.Windows.Size(500, double.PositiveInfinity));
                        block.Arrange(new System.Windows.Rect(0, 0, 500, block.DesiredSize.Height));
                        var position = icon.TranslatePoint(new System.Windows.Point(0, 0), block);
                        if (Math.Abs(position.Y) > 0.1 || Math.Abs(position.X + icon.ActualWidth - block.ActualWidth) > 0.1)
                            throw new InvalidDataException("Copy icon is not at the rendered block's top-right corner.");
                    }
                }
                // Never Show/initialize: no Loaded event or backend invocation.
            } catch (Exception exception) { failure = exception; }
        });
        thread.SetApartmentState(ApartmentState.STA); thread.Start(); thread.Join();
        if (failure is not null) throw new InvalidDataException("UI state transition contract failed.", failure);
        Console.WriteLine("UI retirement contracts passed without backend initialization.");
    }
}
