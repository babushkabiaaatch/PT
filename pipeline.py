chosen = candidates[:MAX_FOR_GEMINI]
    chosen_ids = {video["id"] for video in chosen}
    chosen.extend(
        video for video in candidates
        if video["published"] >= fresh_cutoff and video["id"] not in chosen_ids
    )
    print(f"{len(chosen)} video candidates sent to Gemini.")

    # --- Videos, step C: Gemini judges ---
    labels = load_labels()
    gemini_ok = judge_all(chosen, labels, judge_videos, VIDEO_BATCH)
    if not gemini_ok:
        print("  Gemini could not finish video judging. Unjudged videos will NOT be added.")
        print("  Previously saved videos and previously judged items will be retained.")
    save_labels(labels)

    newly_kept_videos = []
    for video in chosen:
        # Only add videos with an explicit Gemini decision. Never bulk-add unreviewed results.
        label = labels.get(video["id"])
        if not label or not label.get("keep"):
            continue
        category = label.get("cat")
        if category not in ("Goggins", "Longevity"):
            category = video["guess"]
        newly_kept_videos.append({
            "id": video["id"],
            "title": video["title"],
            "channel": video["channel"],
            "seconds": video["seconds"],
            "views": video["views"],
            "published": video["published"],
            "category": category,
            "topic": label.get("topic", ""),
        })

    # Keep older saved videos even if they were not returned by today's search.
    videos = merge_by_id(existing_videos, newly_kept_videos)
    videos.sort(key=lambda video: (-int(video.get("views", 0)), video.get("published", "")))
    write_json_atomic(videos_path, videos)

    goggins_count = sum(1 for video in videos if video.get("category") == "Goggins")
    longevity_count = sum(1 for video in videos if video.get("category") == "Longevity")
    new_videos = sum(
        1 for video in videos
        if (video.get("published") or "") >= fresh_cutoff
    )
    print(
        f"\\nSaved library: {len(videos)} videos "
        f"({goggins_count} Goggins, {longevity_count} Longevity)."
    )
    print(f"  {new_videos} were published in the last {FRESH_DAYS} days.")

    # --- Papers ---
    print()
    newly_kept_papers = []
    paper_candidates_count = 0
    try:
        paper_candidates = fetch_papers()[:MAX_PAPERS_FOR_GEMINI]
        paper_candidates_count = len(paper_candidates)
        if paper_candidates and gemini_ok:
            gemini_ok = judge_all(paper_candidates, labels, judge_papers, PAPER_BATCH)
        elif paper_candidates:
            print("Skipping paper judging because Gemini had a problem; saved papers are retained.")

        if gemini_ok:
            for paper in paper_candidates:
                label = labels.get(paper["id"])
                if not label or not label.get("keep"):
                    continue
                if label.get("cat") not in ("Longevity", "Toughness") or not label.get("summary"):
                    continue
                newly_kept_papers.append({
                    "id": paper["id"],
                    "title": paper["title"],
                    "authors": paper["authors"],
                    "journal": paper["journal"],
                    "date": paper["date"],
                    "url": paper["url"],
                    "preprint": paper["preprint"],
                    "category": label["cat"],
                    "summary": label["summary"],
                    "evidence": label.get("evidence", "other"),
                })
    except Exception as exc:
        print(f"Paper discovery failed; existing papers will be retained: {exc}")

    papers = merge_by_id(existing_papers, newly_kept_papers)
    papers.sort(key=lambda paper: paper.get("date", ""), reverse=True)
    write_json_atomic(papers_path, papers)
    save_labels(labels)
    print(f"Saved research library: {len(papers)} papers ({paper_candidates_count} candidates this run).")

    # Status is written last, after the data files have been saved successfully.
    status = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "videos": len(videos),
        "new_videos": new_videos,
        "papers": len(papers),
        "video_candidates": len(candidates),
        "paper_candidates": paper_candidates_count,
        "gemini_ok": bool(gemini_ok),
    }
    write_json_atomic(status_path, status)
    print("Pipeline completed successfully.")


if __name__ == "__main__":
    main()
