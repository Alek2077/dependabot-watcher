using System;
using System.Windows;
using System.Windows.Controls;

namespace WatcherNativeProof;

// Presentation only. The caller retains the exact reviewed binding/token and
// sends nothing unless this owned modal returns explicit affirmative consent.
internal static class NativeConfirmationDialog
{
    internal static bool Confirm(Window owner, string title, string description, string confirmLabel)
    {
        var dialog = new Window { Owner = owner, Title = title, Width = 640, Height = 460,
            MinWidth = 480, MinHeight = 320, WindowStartupLocation = WindowStartupLocation.CenterOwner,
            FontFamily = owner.FontFamily, FontSize = owner.FontSize, Background = owner.Background,
            ShowInTaskbar = false };
        var root = new DockPanel { Margin = new Thickness(22) };
        var buttons = new StackPanel { Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 16, 0, 0) };
        var cancel = new Button { Content = "Cancel", IsCancel = true,
            Padding = new Thickness(18, 9, 18, 9), Margin = new Thickness(0, 0, 10, 0) };
        cancel.Click += (_, _) => dialog.DialogResult = false;
        var confirm = new Button { Content = confirmLabel, Padding = new Thickness(18, 9, 18, 9),
            FontWeight = FontWeights.SemiBold };
        confirm.Click += (_, _) => dialog.DialogResult = true;
        buttons.Children.Add(cancel); buttons.Children.Add(confirm);
        DockPanel.SetDock(buttons, Dock.Bottom); root.Children.Add(buttons);
        var heading = new TextBlock { Text = title, FontSize = 22, FontWeight = FontWeights.SemiBold,
            TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 16) };
        DockPanel.SetDock(heading, Dock.Top); root.Children.Add(heading);
        root.Children.Add(new TextBox { Text = description, IsReadOnly = true, TextWrapping = TextWrapping.Wrap,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto, BorderThickness = new Thickness(0),
            Background = dialog.Background });
        dialog.Content = root;
        dialog.Loaded += (_, _) => cancel.Focus();
        return dialog.ShowDialog() == true;
    }
}
