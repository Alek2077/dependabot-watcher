using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;

namespace WatcherNativeProof;

public sealed class Recommendation
{
    public int Number { get; set; }
    public string Repository { get; set; } = "";
    public string Title { get; set; } = "";
    public string Head { get; set; } = "";
    public string Status { get; set; } = "Pending";
    public string Message { get; set; } = "";
    public DateTimeOffset? ResumeAt { get; set; }
    public string Label => $"{Repository}  ·  #{Number}";
}

public sealed class ProofWindow : Window
{
    private readonly List<Recommendation> _items;
    private readonly DataGrid _queue = new();
    private readonly TextBlock _detail = new() { TextWrapping = TextWrapping.Wrap, FontSize = 14 };
    private readonly TextBox _message = new() { AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 70 };
    private readonly TextBlock _outcome = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 12, 0, 0) };
    private readonly string _state = Path.Combine(App.Root, "synthetic-queue.json");

    public ProofWindow()
    {
        Title = "Dependabot Watcher — native interaction proof";
        Width = 1080; Height = 700; MinWidth = 850; MinHeight = 540;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        _items = File.Exists(_state)
            ? JsonSerializer.Deserialize<List<Recommendation>>(File.ReadAllText(_state)) ?? throw new InvalidDataException("Invalid proof state")
            : Enumerable.Range(1, 20).Select(n => new Recommendation {
                Number = 100 + n, Repository = $"example/repository-{(n - 1) / 7 + 1}",
                Title = $"Update example dependency {n}",
                Head = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes($"synthetic-{n}"))).ToLowerInvariant()
            }).ToList();
        var root = new DockPanel { Margin = new Thickness(22) };
        var header = new StackPanel { Margin = new Thickness(0, 0, 0, 18) };
        header.Children.Add(new TextBlock { Text = "Recommendations", FontSize = 24, FontWeight = FontWeights.SemiBold });
        header.Children.Add(new TextBlock { Text = "NATIVE PROOF ONLY · Synthetic evidence · No AI calls · No GitHub access", Margin = new Thickness(0, 7, 0, 0) });
        DockPanel.SetDock(header, Dock.Top); root.Children.Add(header);
        var footer = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 15, 0, 0) };
        footer.Children.Add(Button("Send test notification", () => App.SendNotice()));
        footer.Children.Add(Button("Hide to tray", Hide));
        footer.Children.Add(Button("Exit proof", () => System.Windows.Application.Current.Shutdown()));
        DockPanel.SetDock(footer, Dock.Bottom); root.Children.Add(footer);
        var split = new Grid();
        split.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1.15, GridUnitType.Star) });
        split.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        _queue.AutoGenerateColumns = false; _queue.IsReadOnly = true;
        _queue.SelectionMode = DataGridSelectionMode.Single;
        _queue.HeadersVisibility = DataGridHeadersVisibility.Column;
        _queue.CanUserAddRows = false; _queue.Margin = new Thickness(0, 0, 22, 0);
        _queue.Columns.Add(new DataGridTextColumn { Header = "Recommendation", Binding = new Binding("Label"), Width = new DataGridLength(1, DataGridLengthUnitType.Star) });
        _queue.Columns.Add(new DataGridTextColumn {
            Header = "Decision", Binding = new Binding("Status"),
            Width = DataGridLength.Auto, MinWidth = 145
        });
        _queue.ItemsSource = _items;
        _queue.SelectionChanged += (_, _) => Render();
        split.Children.Add(_queue);
        var panel = new StackPanel();
        panel.Children.Add(_detail);
        panel.Children.Add(new TextBlock { Text = "Question or direction (saved, not sent to an agent in this proof)", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 20, 0, 8) });
        panel.Children.Add(_message);
        panel.Children.Add(Button("Save question", () => Decide("Question saved")));
        var choices = new WrapPanel { Margin = new Thickness(0, 15, 0, 0) };
        choices.Children.Add(Button("Simulate approval", () => Decide("Simulated approval")));
        choices.Children.Add(Button("Reject", () => Decide("Rejected")));
        choices.Children.Add(Button("Defer one day", () => Decide("Deferred")));
        panel.Children.Add(choices); panel.Children.Add(_outcome);
        Grid.SetColumn(panel, 1);
        split.Children.Add(new ScrollViewer { Content = panel, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        Grid.SetColumn(split.Children[1], 1);
        root.Children.Add(split); Content = root;
        _queue.SelectedIndex = 0;
        Closing += (_, e) => { e.Cancel = true; Hide(); App.Record("queue_hidden"); };
    }

    private static Button Button(string text, Action action)
    {
        var button = new Button { Content = text, Padding = new Thickness(12, 7, 12, 7), Margin = new Thickness(0, 6, 8, 0) };
        button.Click += (_, _) => action(); return button;
    }

    private void Render()
    {
        if (_queue.SelectedItem is not Recommendation item) return;
        _detail.Text = $"{item.Title}\n\nRepository: {item.Repository}\nPR: #{item.Number}\nHead: {item.Head}\n\nOrdering: deterministic fixture; no model involved.\nEvidence: synthetic passing checks, not a real compatibility assessment.\n\nDecision: {item.Status}";
        _message.Text = item.Message;
        _outcome.Text = item.ResumeAt is { } at ? $"Deferred until {at.ToLocalTime():g}." : "";
    }

    private void Decide(string status)
    {
        if (_queue.SelectedItem is not Recommendation item) return;
        if (status == "Question saved" && string.IsNullOrWhiteSpace(_message.Text)) return;
        if (status == "Simulated approval" && MessageBox.Show(this,
                $"Simulate approval of {item.Repository} PR #{item.Number}?\nNo real action will occur.",
                "Confirm synthetic action", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;
        item.Message = _message.Text;
        if (status != "Question saved")
        {
            item.Status = status;
            item.ResumeAt = status == "Deferred" ? DateTimeOffset.Now.AddDays(1) : null;
        }
        File.WriteAllText(_state + ".tmp", JsonSerializer.Serialize(_items));
        File.Move(_state + ".tmp", _state, true);
        App.Record("synthetic_decision", new { item.Number, item.Status });
        _queue.Items.Refresh(); Render();
        _outcome.Text = status == "Question saved"
            ? "Question saved. No investigator is connected to this proof."
            : $"{status} saved. GitHub remains untouched.";
    }
}
